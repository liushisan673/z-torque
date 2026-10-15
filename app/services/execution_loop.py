from dataclasses import dataclass
from typing import Any
from uuid import UUID
from datetime import datetime, timezone
import asyncio

from app.core.errors import ApiError
from app.kernel.model_gateway import ModelGateway, ModelMessage, ModelRequest, ModelResponse
from app.kernel.skill_gateway import ExecutionResult, SkillGateway
from app.services.function_calling import ToolMapping, map_tool_calls

MAX_ROUNDS = 5
MAX_TOOL_CALLS_PER_ROUND = 8
MAX_TOOL_RESULT_CHARS = 20_000


@dataclass(frozen=True)
class LoopResult:
    response: ModelResponse
    messages: list[ModelMessage]
    rounds: int
    # 写入类工具触发人工审批时的挂起信息（confirmation_id/tool_version_id/arguments/task_id）
    pending_confirmation: dict | None = None


class AgentExecutionLoop:
    def __init__(self, model_gateway: ModelGateway, skill_gateway: SkillGateway) -> None:
        self.model_gateway = model_gateway
        self.skill_gateway = skill_gateway

    async def run(
        self,
        *,
        request: ModelRequest,
        mappings: dict[str, ToolMapping],
        db: Any,
        tenant_id: UUID,
        user_id: UUID,
        task_id: UUID | None = None,
        cancellation_event: asyncio.Event | None = None,
        deadline_at: datetime | None = None,
        delegation_depth: int = 0,
    ) -> LoopResult:
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
            messages.append(ModelMessage("assistant", response.content, tool_calls=[call.__dict__ for call in response.tool_calls]))
            for call, (mapping, arguments) in zip(response.tool_calls, map_tool_calls(response.tool_calls, mappings), strict=True):
                if mapping.child_agent_version_id is not None:
                    # 委派调用：把任务交给子智能体独立执行，其最终回答作为工具结果返回
                    child_content = await self._run_child_agent(
                        db,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        child_agent_version_id=mapping.child_agent_version_id,
                        task_text=str(arguments.get("task", "")).strip(),
                        delegation_depth=delegation_depth,
                        cancellation_event=cancellation_event,
                        deadline_at=deadline_at,
                    )
                    messages.append(ModelMessage("tool", child_content, tool_call_id=call.id))
                    continue
                result: ExecutionResult = await self.skill_gateway.execute_async(
                    db,
                    tenant_id=tenant_id,
                    user_id=user_id,
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
                    info = dict(result.data or {})
                    info.setdefault("tool_version_id", str(mapping.tool_version_id))
                    info["arguments"] = arguments
                    info["task_id"] = str(task_id) if task_id else None
                    return LoopResult(response, messages, round_number, pending_confirmation=info)
                tool_content = self._tool_result(result)
                messages.append(ModelMessage("tool", tool_content, tool_call_id=call.id))
        raise ApiError("AI_MAX_ROUNDS_EXCEEDED", "maximum model execution rounds exceeded", 422)

    async def _run_child_agent(
        self,
        db: Any,
        *,
        tenant_id: UUID,
        user_id: UUID,
        child_agent_version_id: UUID,
        task_text: str,
        delegation_depth: int,
        cancellation_event: asyncio.Event | None,
        deadline_at: datetime | None,
    ) -> str:
        """执行子智能体：以子版本 system_prompt + 委派任务构建独立会话，跑一轮完整执行循环。"""
        import json

        from app.config import get_settings
        from app.kernel.task.runtime import TaskRuntime
        from app.models import Agent, AgentVersion

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
        mappings, tools = TaskRuntime._build_tool_mappings(db, child, tenant_id)
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
        child_request = ModelRequest(
            trace_id=f"delegate-{child_agent_version_id}-{delegation_depth}",
            conversation_id=None,
            agent_version_id=child_agent_version_id,
            model=settings.model_name,
            messages=child_messages,
            tools=tools,
            timeout_seconds=settings.model_timeout_seconds,
        )
        try:
            if tools:
                loop_result = await AgentExecutionLoop(self.model_gateway, self.skill_gateway).run(
                    request=child_request,
                    mappings=mappings,
                    db=db,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    cancellation_event=cancellation_event,
                    deadline_at=deadline_at,
                    delegation_depth=delegation_depth + 1,
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
