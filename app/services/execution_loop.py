import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import update

from app.core.errors import ApiError
from app.kernel.model_gateway import (
    ModelGateway,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
)
from app.kernel.ports.model_selector import ModelSelector, resolve_model_selection
from app.kernel.ports.skill_executor import ExecutionResult
from app.kernel.security_context import SecurityContext
from app.kernel.skill_gateway import SkillGateway
from app.kernel.task.checkpoints import latest_checkpoint
from app.kernel.task.service import append_task_event
from app.models import Task, TaskCheckpoint
from app.services.function_calling import ToolMapping, map_tool_calls
from app.services.model_limits import effective_max_tokens
from app.services.tool_mapping_builder import build_tool_mappings

MAX_ROUNDS = 5
MAX_TOOL_CALLS_PER_ROUND = 8
MAX_TOOL_RESULT_CHARS = 20_000


def _serialize_message(m: ModelMessage) -> dict:
    return {"role": m.role, "content": m.content, "tool_call_id": m.tool_call_id, "tool_calls": m.tool_calls, "reasoning_content": getattr(m, "reasoning_content", None)}


def _brief(value: Any, limit: int = 1000) -> str:
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


@dataclass(frozen=True)
class LoopResult:
    response: ModelResponse
    messages: list[ModelMessage]
    rounds: int
    # 写入类工具触发人工审批时的挂起信息（confirmation_id/tool_version_id/arguments/task_id）
    pending_confirmation: dict | None = None


class AgentExecutionLoop:
    def __init__(self, model_gateway: ModelGateway, skill_gateway: SkillGateway, model_selector: ModelSelector | None = None) -> None:
        self.model_gateway = model_gateway
        self.skill_gateway = skill_gateway
        # model.selector：子智能体委派时按子版本快照绑定选模型（文档 §6.1）
        self.model_selector = model_selector

    async def run(
        self,
        *,
        request: ModelRequest,
        mappings: dict[str, ToolMapping],
        db: Any,
        ctx: SecurityContext,
        task_id: UUID | None = None,
        cancellation_event: asyncio.Event | None = None,
        deadline_at: datetime | None = None,
        delegation_depth: int = 0,
        resume: dict | None = None,
    ) -> LoopResult:
        task = db.get(Task, task_id) if task_id else None
        if resume is not None:
            # 审批恢复续跑：从挂起时的上下文快照重建消息，回填已重放的工具结果后继续循环
            messages = [ModelMessage(**m) for m in resume.get("messages", [])]
            messages.append(ModelMessage("tool", resume.get("tool_result") or "", tool_call_id=resume.get("tool_call_id")))
        else:
            messages = list(request.messages)
        for round_number in range(1, MAX_ROUNDS + 1):
            if cancellation_event is not None and cancellation_event.is_set():
                raise ApiError("AI_TASK_NOT_CANCELABLE", "task cancellation requested", 409)
            timeout = request.timeout_seconds
            if deadline_at is not None:
                timeout = min(timeout, (deadline_at - datetime.now(timezone.utc)).total_seconds())
                if timeout <= 0:
                    raise ApiError("AI_TASK_TIMEOUT", "task deadline exceeded", 504)
            response = await self.model_gateway.generate(
                ModelRequest(**{**request.__dict__, "messages": messages, "round_number": round_number, "timeout_seconds": timeout})
            )
            if not response.tool_calls:
                if response.content is None:
                    raise ApiError("AI_MODEL_RESPONSE_INVALID", "model returned neither text nor tool calls", 502)
                return LoopResult(response, messages, round_number)
            if len(response.tool_calls) > MAX_TOOL_CALLS_PER_ROUND:
                raise ApiError("AI_FUNCTION_CALL_INVALID", "too many tool calls in one round", 422)
            messages.append(ModelMessage("assistant", response.content, tool_calls=[call.__dict__ for call in response.tool_calls], reasoning_content=response.reasoning_content))
            for call, (mapping, arguments) in zip(response.tool_calls, map_tool_calls(response.tool_calls, mappings), strict=True):
                if task is not None:
                    append_task_event(db, task, "TASK_TOOL_CALL", f"calling tool {call.name}", trace_id=request.trace_id, payload_summary={"tool_name": call.name, "arguments": _brief(arguments)})
                if mapping.child_agent_version_id is not None:
                    # 委派调用：把任务交给子智能体独立执行，其最终回答作为工具结果返回
                    child_content = await self._run_child_agent(
                        db,
                        ctx=ctx,
                        child_agent_version_id=mapping.child_agent_version_id,
                        task_text=str(arguments.get("task", "")).strip(),
                        delegation_depth=delegation_depth,
                        cancellation_event=cancellation_event,
                        deadline_at=deadline_at,
                        task_id=task_id,
                    )
                    messages.append(ModelMessage("tool", child_content, tool_call_id=call.id))
                    if task is not None:
                        append_task_event(db, task, "TASK_TOOL_RESULT", f"tool {call.name} returned", trace_id=request.trace_id, payload_summary={"tool_name": call.name, "status": "SUCCEEDED", "result": _brief(child_content)})
                    continue
                result: ExecutionResult = await self.skill_gateway.execute_async(
                    db,
                    ctx,
                    skill_version_id=mapping.skill_version_id,
                    tool_version_id=mapping.tool_version_id,
                    arguments=arguments,
                    task_id=task_id,
                    cancellation_event=cancellation_event,
                    deadline_at=deadline_at,
                )
                if result.status == "WAITING_HUMAN":
                    # 写入类工具触发人工审批：任务已暂停（checkpoint 挂起），循环立即结束；
                    # 审批通过后由任务队列重放该次工具调用，调用方据此返回等待审批状态。
                    # 持久化执行循环上下文（消息快照 + 挂起工具调用 id），恢复时回填结果续跑至最终回复。
                    info = dict(result.data or {})
                    info.setdefault("tool_version_id", str(mapping.tool_version_id))
                    info["arguments"] = arguments
                    info["task_id"] = str(task_id) if task_id else None
                    if task is not None:
                        checkpoint = latest_checkpoint(db, task_id)
                        if checkpoint is not None:
                            context = dict(checkpoint.execution_context or {})
                            context["chat_context"] = {
                                "messages": [_serialize_message(m) for m in messages],
                                "tool_call_id": call.id,
                            }
                            # JSON 列用 update 强制落库（ORM 实例属性赋值在部分场景不触发变更跟踪）
                            db.execute(update(TaskCheckpoint).where(TaskCheckpoint.id == checkpoint.id).values(execution_context=context))
                            db.flush()
                    return LoopResult(response, messages, round_number, pending_confirmation=info)
                tool_content = self._tool_result(result)
                messages.append(ModelMessage("tool", tool_content, tool_call_id=call.id))
                if task is not None:
                    if result.status == "SUCCEEDED":
                        append_task_event(db, task, "TASK_TOOL_RESULT", f"tool {call.name} returned", trace_id=request.trace_id, payload_summary={"tool_name": call.name, "status": result.status, "result": _brief(result.data)})
                    else:
                        append_task_event(db, task, "TASK_TOOL_RESULT", f"tool {call.name} failed: {result.error_code}", trace_id=request.trace_id, payload_summary={"tool_name": call.name, "status": result.status, "error": result.error_message})
        raise ApiError("AI_MAX_ROUNDS_EXCEEDED", "maximum model execution rounds exceeded", 422)

    async def _run_child_agent(
        self,
        db: Any,
        *,
        ctx: SecurityContext,
        child_agent_version_id: UUID,
        task_text: str,
        delegation_depth: int,
        cancellation_event: asyncio.Event | None,
        deadline_at: datetime | None,
        task_id: UUID | None = None,
    ) -> str:
        """执行子智能体：以子版本 system_prompt + 委派任务构建独立会话，跑一轮完整执行循环。

        `task_id` 透传父任务标识：子智能体的工具调用同样计入该任务的
        SkillExecutionLog 与任务事件流，保证「每次执行必须记账」（契约红线1）。
        """
        import json

        from app.config import get_settings
        from app.models import Agent, AgentVersion

        tenant_id = ctx.tenant_id

        def _error(message: str) -> str:
            return json.dumps({"status": "FAILED", "error": message}, ensure_ascii=True)

        settings = get_settings()
        if delegation_depth >= settings.delegation_max_depth:
            return _error("delegation depth exceeded")
        if not task_text:
            return _error("delegated task text is empty")
        child = db.get(AgentVersion, child_agent_version_id)
        if child is None or child.tenant_id != tenant_id or child.status != "PUBLISHED":
            return _error("child agent version is not available")
        child_agent = db.get(Agent, child.agent_id)
        if child_agent is None or child_agent.status != "ACTIVE":
            return _error("child agent is not available")
        mappings, tools = build_tool_mappings(db, child, tenant_id, exclude_requires_confirmation=True)
        # 与 chat 主路径一致：注入子智能体绑定 skill 的执行指令
        system_prompt = (child.config_snapshot or {}).get("system_prompt")
        if mappings:
            from app.models import SkillVersion as _SV
            seen: set[UUID] = set()
            instructions: list[str] = []
            for m in mappings.values():
                if m.child_agent_version_id is not None or m.skill_version_id in seen:
                    continue
                seen.add(m.skill_version_id)
                sv = db.get(_SV, m.skill_version_id)
                if sv is not None and sv.instructions and sv.instructions.strip():
                    instructions.append(sv.instructions.strip())
            if instructions:
                system_prompt = (system_prompt + "\n\n" if system_prompt else "") + "\n\n".join(instructions)
        child_messages = [ModelMessage("system", system_prompt)] if system_prompt else []
        child_messages.append(ModelMessage("user", task_text))
        # 子版本快照绑定优先（模型 + 调用参数），未设置回退模型档案 max_output_tokens，再退全局兜底（文档 §6.1）
        child_snapshot = child.config_snapshot or {}
        child_selection = resolve_model_selection(
            self.model_selector,
            agent_version_id=child_agent_version_id,
            snapshot_model=child_snapshot.get("model"),
            snapshot_temperature=child_snapshot.get("temperature"),
            snapshot_max_tokens=child_snapshot.get("max_tokens"),
            default_model=settings.model_name,
            default_temperature=settings.model_temperature,
            default_max_tokens=settings.model_max_tokens,
        )
        child_selection = replace(
            child_selection,
            max_tokens=effective_max_tokens(
                db,
                tenant_id=ctx.tenant_id,
                model_identifier=child_selection.model,
                snapshot_max_tokens=child_snapshot.get("max_tokens"),
                default_max_tokens=settings.model_max_tokens,
            ),
        )
        child_request = ModelRequest(
            trace_id=f"delegate-{child_agent_version_id}-{delegation_depth}",
            conversation_id=None,
            agent_version_id=child_agent_version_id,
            model=child_selection.model,
            temperature=child_selection.temperature,
            max_tokens=child_selection.max_tokens,
            messages=child_messages,
            tools=tools,
            timeout_seconds=settings.model_timeout_seconds,
        )
        try:
            if tools:
                loop_result = await AgentExecutionLoop(self.model_gateway, self.skill_gateway, self.model_selector).run(
                    request=child_request,
                    mappings=mappings,
                    db=db,
                    ctx=ctx,
                    cancellation_event=cancellation_event,
                    deadline_at=deadline_at,
                    delegation_depth=delegation_depth + 1,
                    task_id=task_id,
                )
                content = loop_result.response.content
            else:
                response = await self.model_gateway.generate(child_request)
                content = response.content
        except ModelGatewayError as exc:
            return _error(f"child agent execution failed: {exc.message}")
        except ApiError as exc:
            return _error(f"child agent execution failed: {exc.message}")
        if not content or not content.strip():
            return _error("child agent returned an empty response")
        return content

    @staticmethod
    def _tool_result(result: ExecutionResult) -> str:
        import json
        payload = {"status": result.status, "data": result.data, "error_code": result.error_code, "error_message": result.error_message}
        return json.dumps(payload, ensure_ascii=True, default=str)[:MAX_TOOL_RESULT_CHARS]
