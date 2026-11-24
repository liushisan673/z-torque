import asyncio
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, update

from app.core.errors import ApiError
from app.kernel.audit.service import record_audit
from app.kernel.model_gateway import (
    ModelContentPart,
    ModelGateway,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
)
from app.kernel.ports.model_selector import ModelSelector, resolve_model_selection
from app.kernel.ports.skill_executor import ExecutionResult
from app.kernel.registry.active import call_optional
from app.kernel.security_context import SecurityContext
from app.kernel.skill_gateway import SkillGateway
from app.kernel.task.checkpoints import latest_checkpoint
from app.kernel.task.service import append_task_event
from app.models import Agent, AgentVersion, Task, TaskCheckpoint
from app.repositories import visible_owner_clause
from app.services.context_governance import heal_context, sanitize_untrusted
from app.services.function_calling import ToolMapping, repair_tool_message, validate_tool_calls
from app.services.model_limits import effective_max_tokens
from app.services.tool_mapping_builder import build_tool_mappings

# 单次执行循环的最大「模型思考 + 工具调用」轮次。8 轮对多工具 MCP 链路偏紧，
# 容易在收尾阶段触发 AI_MAX_ROUNDS_EXCEEDED；放宽到 30，仍由任务 deadline 兜底。
MAX_ROUNDS = 30
MAX_TOOL_CALLS_PER_ROUND = 8
MAX_TOOL_RESULT_CHARS = 20_000
# 站内 A2A 简单请求默认只派发一个专业 Agent；避免模型同轮/跨轮重复调度。
MAX_A2A_DISPATCHES_PER_RUN = 1


def _serialize_message(m: ModelMessage) -> dict:
    """消息快照（JSON 可序列化）：审批恢复与决策轮种子共用。

    多模态片段必须一并序列化：漏掉 content_parts 会让恢复/续跑后的模型看不到历史图片，
    表现为"同一段对话里前文图片突然失效"。
    """
    parts = [
        {"type": part.type, "text": part.text, "image_url": part.image_url}
        for part in (m.content_parts or [])
    ]
    return {
        "role": m.role,
        "content": m.content,
        "tool_call_id": m.tool_call_id,
        "tool_calls": m.tool_calls,
        "reasoning_content": getattr(m, "reasoning_content", None),
        "content_parts": parts or None,
    }


def _deserialize_message(raw: dict) -> ModelMessage:
    data = dict(raw)
    parts = data.pop("content_parts", None)
    if parts:
        data["content_parts"] = [ModelContentPart(**part) for part in parts]
    return ModelMessage(**data)


def _brief(value: Any, limit: int = 1000) -> str:
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def serialize_seed(*, messages: list[ModelMessage], response: ModelResponse) -> dict:
    """把「流式决策轮已经拿到的模型结论」序列化进任务，供 worker 直接执行工具。

    背景：/chat/stream 已经用同一份消息 + 同一组工具跑完了一轮推理（模型返回 tool_calls），
    随后才创建任务；worker 若再从第 1 轮重跑一遍，等于把同一轮模型调用付两次钱、等两次延迟。
    这里把结论冻结成任务种子，worker 跳过决策轮直接执行工具，再从第 2 轮继续推理。
    """
    return {
        "messages": [_serialize_message(m) for m in messages],
        "assistant": {
            "content": response.content,
            "tool_calls": [dict(call.__dict__) for call in response.tool_calls],
            "reasoning_content": response.reasoning_content,
        },
        "finish_reason": response.finish_reason,
    }


def seed_round(seed: dict | None, mappings: dict[str, ToolMapping]) -> tuple[list[ModelMessage], ModelResponse] | None:
    """校验并还原种子；不可用（工具已解绑、结构损坏）时返回 None，让执行循环照常推理。

    种子只是省掉一次重复推理，绝不能改变语义：任何一处对不上就当作没给种子。
    """
    if not isinstance(seed, dict):
        return None
    try:
        messages = [_deserialize_message(m) for m in seed.get("messages") or []]
        assistant = seed.get("assistant") or {}
        calls = [
            ModelToolCall(id=str(call.get("id") or ""), name=str(call.get("name") or ""), arguments=dict(call.get("arguments") or {}))
            for call in (assistant.get("tool_calls") or [])
        ]
    except (TypeError, ValueError, AttributeError):
        return None
    if not messages or not calls or any(call.name not in mappings for call in calls):
        return None
    return messages, ModelResponse(
        content=assistant.get("content"),
        tool_calls=calls,
        finish_reason=seed.get("finish_reason") or "tool_calls",
        reasoning_content=assistant.get("reasoning_content"),
    )


def _progress_event(db: Any, task: Task | None, event_type: str, summary: str, *, trace_id: str, payload: dict | None = None) -> None:
    """写一条「过程事件」并立刻提交。

    worker 的整个执行体共用一个事务：不提交时，/tasks/{id}/stream 的轮询会话看不到任何事件，
    用户只能等任务结束才一次性看到全部过程（这正是「执行中页面不动」的根因）。
    过程事件不参与状态一致性判断，提前提交不会破坏状态机不变量。
    """
    if task is None:
        return
    append_task_event(db, task, event_type, summary, trace_id=trace_id, payload_summary=payload)
    commit = getattr(db, "commit", None)
    if commit is not None:
        commit()


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
        seed: dict | None = None,
    ) -> LoopResult:
        task = db.get(Task, task_id) if task_id else None
        repair_used = False
        repaired_ids: set[str] = set()
        a2a_dispatch_count = 0
        seeded: tuple[list[ModelMessage], ModelResponse] | None = None
        if resume is not None:
            # 审批恢复续跑：从挂起时的上下文快照重建消息，回填已重放的工具结果后继续循环
            messages = [_deserialize_message(m) for m in resume.get("messages", [])]
            messages.append(ModelMessage("tool", resume.get("tool_result") or "", tool_call_id=resume.get("tool_call_id")))
        else:
            # 决策轮种子（/chat/stream 已完成的那一轮）：命中则跳过重复推理，直接从工具执行开始
            seeded = seed_round(seed, mappings)
            messages = seeded[0] if seeded is not None else list(request.messages)
        for round_number in range(1, MAX_ROUNDS + 1):
            # §2.4 自愈管线：每次模型调用前修复畸形 tool call / 孤儿 result，
            # 保证 tool_calls 与 toolResult 成对，且超长结果不撑爆上下文。
            messages = heal_context(messages, tool_result_max_chars=MAX_TOOL_RESULT_CHARS)
            if cancellation_event is not None and cancellation_event.is_set():
                raise ApiError("AI_TASK_NOT_CANCELABLE", "task cancellation requested", 409)
            timeout = request.timeout_seconds
            if deadline_at is not None:
                timeout = min(timeout, (deadline_at - datetime.now(timezone.utc)).total_seconds())
                if timeout <= 0:
                    raise ApiError("AI_TASK_TIMEOUT", "task deadline exceeded", 504)
            if round_number == 1 and seeded is not None:
                # 复用决策轮结论：v1 的 message.delta 已经把这个过程实时推给用户了，不再补事件
                response = seeded[1]
            else:
                # 「模型推理中」先落库再调用：一轮推理动辄数秒，用户必须能看见当前在等什么。
                # 推理完成不再单独发事件——紧随其后的工具调用/终态事件本身就是它的结束标志，
                # 少一条重复的「模型推理完成」时间线也更好读。
                _progress_event(db, task, "TASK_MODEL_STARTED", f"model round {round_number} started", trace_id=request.trace_id, payload={"round": round_number})
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
            validated_calls = validate_tool_calls(response.tool_calls, mappings)
            invalid_calls = [item for item in validated_calls if item.error]
            if invalid_calls:
                if repair_used:
                    for item in invalid_calls:
                        record_audit(db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="TOOL_CALL_REPAIR_FAILED", resource_type="TASK", resource_id=task_id, summary="tool argument repair exhausted", detail={"status": "FAILED", "error_code": item.error["code"], "tool_version_id": str(item.mapping.tool_version_id) if item.mapping and item.mapping.tool_version_id else None, "reason": f"{item.error['json_path']}: {item.error['message']}"})
                    raise ApiError("AI_FUNCTION_CALL_INVALID", "tool arguments still invalid after one repair round", 422)
                repair_used = True
                repaired_ids: set[str] = set()
                for item in validated_calls:
                    if item.error:
                        repaired_ids.add(item.call.id)
                        record_audit(db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="TOOL_ARGUMENT_INVALID", resource_type="TASK", resource_id=task_id, summary="tool arguments failed strict validation", detail={"status": "FAILED", "error_code": item.error["code"], "tool_version_id": str(item.mapping.tool_version_id) if item.mapping and item.mapping.tool_version_id else None, "reason": f"{item.error['json_path']}: {item.error['message']}"})
                        messages.append(ModelMessage("tool", repair_tool_message(item.call, item.error), tool_call_id=item.call.id))
                        _progress_event(db, task, "TASK_TOOL_ARGUMENT_INVALID", f"invalid arguments for {item.call.name}", trace_id=request.trace_id, payload={"tool_name": item.call.name, "json_path": item.error["json_path"]})
                    else:
                        messages.append(ModelMessage("tool", "SKIPPED：同一轮其它工具调用参数校验失败，本调用未执行；请在修复回合重新发起。", tool_call_id=item.call.id))
                continue
            for item in validated_calls:
                call = item.call
                mapping = item.mapping
                arguments = item.arguments
                if mapping is None:
                    raise ApiError("AI_TOOL_MAPPING_INVALID", "tool name is not allowed for this agent version", 422)
                _progress_event(db, task, "TASK_TOOL_CALL", f"calling tool {call.name}", trace_id=request.trace_id, payload={"tool_name": call.name, "arguments": _brief(arguments)})
                dispatch_target = mapping.child_agent_version_id or mapping.dispatch_agent_version_id
                if dispatch_target is not None:
                    is_site_a2a = mapping.dispatch_agent_version_id is not None
                    if is_site_a2a and a2a_dispatch_count >= MAX_A2A_DISPATCHES_PER_RUN:
                        child_result = {
                            "status": "SKIPPED",
                            "agent_version_id": str(dispatch_target),
                            "error_code": "AI_A2A_DISPATCH_LIMIT",
                            "error_message": "本次任务已派发一个专业智能体；为避免重复调度，本次调用未执行。",
                        }
                    else:
                        if is_site_a2a:
                            a2a_dispatch_count += 1
                        child_result = await self._run_child_agent(
                            db,
                            ctx=ctx,
                            child_agent_version_id=dispatch_target,
                            task_text=str(arguments.get("task", "")).strip(),
                            delegation_depth=delegation_depth,
                            require_current_version=is_site_a2a,
                            cancellation_event=cancellation_event,
                            deadline_at=deadline_at,
                            task_id=task_id,
                        )
                    # 结构化结果保留成功/失败状态，父 Agent 不再把子 Agent 失败伪装成成功文本。
                    child_content = json.dumps(child_result, ensure_ascii=True, default=str)[:MAX_TOOL_RESULT_CHARS]
                    messages.append(ModelMessage("tool", child_content, tool_call_id=call.id))
                    child_status = child_result.get("status", "FAILED")
                    _progress_event(
                        db,
                        task,
                        "TASK_TOOL_RESULT",
                        f"tool {call.name} returned ({child_status})",
                        trace_id=request.trace_id,
                        payload={
                            "tool_name": call.name,
                            "agent_version_id": str(dispatch_target),
                            "status": child_status,
                            "error_code": child_result.get("error_code"),
                            "result": _brief(child_result.get("content") or child_result.get("error_message") or child_content),
                        },
                    )
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
                if result.status == "SUCCEEDED":
                    if call.id in repaired_ids:
                        record_audit(db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="TOOL_CALL_REPAIRED", resource_type="TASK", resource_id=task_id, summary="tool call repaired in one round", detail={"status": "SUCCEEDED", "tool_version_id": str(mapping.tool_version_id) if mapping.tool_version_id else None, "reason": "arguments fixed after structured feedback"})
                    _progress_event(db, task, "TASK_TOOL_RESULT", f"tool {call.name} returned", trace_id=request.trace_id, payload={"tool_name": call.name, "status": result.status, "result": _brief(result.data)})
                else:
                    _progress_event(db, task, "TASK_TOOL_RESULT", f"tool {call.name} failed: {result.error_code}", trace_id=request.trace_id, payload={"tool_name": call.name, "status": result.status, "error": result.error_message})
        raise ApiError("AI_MAX_ROUNDS_EXCEEDED", "maximum model execution rounds exceeded", 422)

    async def _run_child_agent(
        self,
        db: Any,
        *,
        ctx: SecurityContext,
        child_agent_version_id: UUID,
        task_text: str,
        delegation_depth: int,
        require_current_version: bool = False,
        cancellation_event: asyncio.Event | None,
        deadline_at: datetime | None,
        task_id: UUID | None = None,
    ) -> dict[str, Any]:
        """执行子智能体：以子版本 system_prompt + 委派任务构建独立会话，跑一轮完整执行循环。

        `task_id` 透传父任务标识：子智能体的工具调用同样计入该任务的
        SkillExecutionLog 与任务事件流，保证「每次执行必须记账」（契约红线1）。
        """
        from app.config import get_settings

        tenant_id = ctx.tenant_id

        def _error(message: str, code: str = "AI_A2A_CHILD_FAILED") -> dict[str, Any]:
            return {
                "status": "FAILED",
                "agent_version_id": str(child_agent_version_id),
                "error_code": code,
                "error_message": message,
            }

        settings = get_settings()
        if delegation_depth >= settings.delegation_max_depth:
            return _error("delegation depth exceeded")
        if not task_text:
            return _error("delegated task text is empty")
        # 执行时二次校验：候选构建后可能发生版本发布/归档/权限变化，不能只信任模型工具映射。
        child_query = (
            select(AgentVersion)
            .join(Agent, Agent.id == AgentVersion.agent_id)
            .where(
                AgentVersion.id == child_agent_version_id,
                AgentVersion.tenant_id == tenant_id,
                AgentVersion.status == "PUBLISHED",
                Agent.status == "ACTIVE",
                visible_owner_clause(Agent, tenant_id, ctx.user_id),
            )
        )
        # 站内 A2A 是新请求的发现路由，必须使用当前发布版；
        # 显式父子委派则遵守父 AgentVersion 快照白名单，可继续执行快照固定的旧发布版。
        if require_current_version:
            child_query = child_query.where(Agent.current_published_version_id == AgentVersion.id)
        child = db.scalar(child_query)
        if child is None:
            return _error("child agent version is not current, published, or visible", "AI_AGENT_NOT_FOUND")
        child_agent = db.get(Agent, child.agent_id)
        if child_agent is None:
            return _error("child agent is not available", "AI_AGENT_NOT_FOUND")
        mappings, tools = build_tool_mappings(db, child, tenant_id, exclude_requires_confirmation=True, user_id=ctx.user_id)
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
        parent_task = db.get(Task, task_id) if task_id else None
        conversation_id = getattr(parent_task, "conversation_id", None)
        if getattr(settings, "memory_long_enabled", False) and hasattr(db, "scalars"):
            bundle = call_optional(
                db,
                audit_tenant_id=tenant_id,
                port_name="memory.long",
                method="recall_bundle",
                actor_user_id=ctx.user_id,
                tenant_id=tenant_id,
                user_id=ctx.user_id,
                agent_id=child.agent_id,
                conversation_id=conversation_id,
                pass_db=True,
                trace_id=ctx.trace_id,
            ) or {}
            scope_labels = {"AGENT": "智能体指令", "USER": "用户画像", "CONVERSATION": "会话记忆"}
            for scope in ("AGENT", "USER", "CONVERSATION"):
                lines = [value for key, value in bundle.items() if key.startswith(f"{scope}:") and value]
                if lines:
                    child_messages.append(ModelMessage("system", f"Delegated memory / {scope_labels[scope]} (untrusted context):\n" + sanitize_untrusted("\n".join(lines))))
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
            tenant_id=ctx.tenant_id,
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
            return _error(f"child agent execution failed: {exc.message}", getattr(exc, "code", "AI_MODEL_GATEWAY_ERROR"))
        except ApiError as exc:
            return _error(f"child agent execution failed: {exc.message}", exc.code)
        if not content or not content.strip():
            return _error("child agent returned an empty response", "AI_A2A_CHILD_FAILED")
        return {
            "status": "SUCCEEDED",
            "agent_version_id": str(child_agent_version_id),
            "content": content,
        }

    @staticmethod
    def _tool_result(result: ExecutionResult) -> str:
        import json
        payload = {"status": result.status, "data": result.data, "error_code": result.error_code, "error_message": result.error_message}
        return json.dumps(payload, ensure_ascii=True, default=str)[:MAX_TOOL_RESULT_CHARS]
