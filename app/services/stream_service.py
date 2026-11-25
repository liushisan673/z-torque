import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.kernel.audit.service import record_audit
from app.kernel.model_gateway import ModelGatewayError
from app.kernel.model_gateway.contracts import ModelRequest, normalize_usage
from app.kernel.security_context import SecurityContext
from app.models import Agent, AgentVersion, Conversation, Message
from app.repositories import get_owned, get_owned_version
from app.services import knowledge_service
from app.services.chat_application import _maybe_summarize, create_chat_task
from app.services.context_builder import assistant_reasoning_parts
from app.services.execution_loop import serialize_seed
from app.services.usage_service import record_usage_summary

logger = logging.getLogger("z_torque.chat_stream")


def _owned_stream_resources(db, *, tenant_id, user_id, conversation_id, user_message_id, agent_version_id):
    """流式写入前的属主收口（Phase 2 §四）：会话属主 + 消息归属该会话 + 版本属主可见。

    Message 表无 owner 列，属主由 Conversation 承担；版本表同理经 Agent 判定。
    任一校验失败抛 404，调用方把它转成 SSE error，绝不写他人会话。
    """
    conversation = get_owned(
        db, Conversation, resource_id=conversation_id, tenant_id=tenant_id,
        user_id=user_id, label="conversation",
    )
    message = db.scalar(
        select(Message).where(
            Message.id == user_message_id,
            Message.tenant_id == tenant_id,
            Message.conversation_id == conversation.id,
        )
    )
    if message is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "message not found", 404)
    agent_version = (
        get_owned_version(
            db, AgentVersion, Agent, "agent_id", version_id=agent_version_id,
            tenant_id=tenant_id, user_id=user_id, label="agent version",
        )
        if agent_version_id
        else None
    )
    return conversation, message, agent_version


def sse_event(event_id: str, event: str, data: dict[str, Any]) -> str:
    return f"id: {event_id}\nevent: {event}\ndata: {json.dumps(data, ensure_ascii=True, default=str)}\n\n"


async def stream_chat(*, gateway, request: ModelRequest, ctx: SecurityContext, conversation_id: Any, user_message_id: str, idempotency_key: str | None, trace_id: str | None, short_memory=None, knowledge_chunks=None) -> AsyncIterator[str]:
    """Chat 流式统一入口（阶段05 2.6 分流）：

    - 流式期间透传 message.delta；
    - 结束后按模型返回分流：纯文本=闲聊落库（message.result + message.done，不创建任务）；
      tool_calls=创建会话任务并入队（message.task_created，前端切换到任务事件流）；
    - 模型错误：message.error 并把触发消息标记 FAILED。
    """
    tenant_id = ctx.tenant_id
    user_id = ctx.user_id
    trace_id = trace_id or ctx.trace_id
    sequence = 0

    def ev(event: str, data: dict[str, Any]) -> str:
        nonlocal sequence
        sequence += 1
        return sse_event(str(sequence), event, data)

    final_response = None
    try:
        async for chunk in gateway.stream_response(request):
            if chunk.get("type") == "delta":
                yield ev("message.delta", {"content": chunk.get("content")})
            elif chunk.get("type") == "final":
                final_response = chunk.get("response")
    except ModelGatewayError as exc:
        yield ev("message.error", {"code": exc.code, "message": exc.message})
        try:
            with SessionLocal() as db:
                # 失败审计：与任务链路 MODEL_CALL_FAILED 对应，审计页可追溯流式失败原因
                record_audit(
                    db,
                    tenant_id=tenant_id,
                    action="MODEL_CALL_FAILED",
                    resource_type="CONVERSATION",
                    resource_id=conversation_id if isinstance(conversation_id, UUID) else None,
                    actor_user_id=user_id,
                    summary="model stream failed",
                    detail={"status": "failed", "error_code": exc.code, "error_message": exc.message, "stream": True},
                    trace_id=trace_id,
                )
                # 属主收口：失败标记也只允许写本人会话下的消息，禁止越权改写他人消息
                conversation = db.scalar(
                    select(Conversation).where(
                        Conversation.id == conversation_id,
                        Conversation.tenant_id == tenant_id,
                        Conversation.owner_user_id == user_id,
                    )
                )
                if conversation is not None:
                    user_message = db.scalar(
                        select(Message).where(
                            Message.id == user_message_id,
                            Message.tenant_id == tenant_id,
                            Message.conversation_id == conversation.id,
                        )
                    )
                    if user_message is not None:
                        user_message.status = "FAILED"
                db.commit()
        except Exception:
            logger.exception("failed to mark stream message %s as FAILED", user_message_id)
        return
    if final_response is None:
        yield ev("message.error", {"code": "AI_MODEL_RESPONSE_INVALID", "message": "model stream returned no response"})
        return
    try:
        with SessionLocal() as db:
            try:
                conversation, user_message, agent_version = _owned_stream_resources(
                    db,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    user_message_id=user_message_id,
                    agent_version_id=request.agent_version_id,
                )
            except ApiError:
                yield ev("message.error", {"code": "AI_RESOURCE_NOT_FOUND", "message": "conversation or message not found"})
                return
            record_usage_summary(
                db,
                tenant_id=tenant_id,
                agent_id=agent_version.agent_id if agent_version else None,
                model=request.model or "unknown",
                usage=final_response.usage,
            )

            def audit_model_call(action: str, summary: str, detail: dict[str, Any]) -> None:
                """流式模型调用审计：与任务链路 MODEL_CALL_* 对应，补齐此前完全缺失的记录。"""
                record_audit(
                    db,
                    tenant_id=tenant_id,
                    action=action,
                    resource_type="CONVERSATION",
                    resource_id=conversation.id,
                    actor_user_id=user_id,
                    summary=summary,
                    detail={"stream": True, "finish_reason": final_response.finish_reason, "tools": len(request.tools or []), **detail},
                    trace_id=trace_id,
                )

            if final_response.tool_calls:
                # 分流：模型要求调用工具 → 创建会话任务并入队（Outbox TASK_READY），执行由 worker 完成。
                # 本轮推理结论随任务一起落库（种子），worker 直接执行工具、从第 2 轮继续推理：
                # 否则同一份消息 + 同一组工具会被完整推理两遍，用户要多等一整轮模型延迟。
                audit_model_call(
                    "MODEL_CALL_COMPLETED",
                    "model call completed (tool calls)",
                    {
                        "provider_request_id": final_response.provider_request_id,
                        "usage": normalize_usage(final_response.usage),
                        "model": request.model or "unknown",
                        "tool_call_count": len(final_response.tool_calls),
                        "task_created": True,
                    },
                )
                seed = serialize_seed(messages=list(request.messages), response=final_response)
                task = create_chat_task(db, ctx=ctx, conversation=conversation, user_message=user_message, idempotency_key=idempotency_key or str(user_message_id), trace_id=trace_id, seed=seed)
                conversation.last_message_at = datetime.now(timezone.utc)
                db.commit()
                yield ev("message.task_created", {"task_id": str(task.id), "conversation_id": str(conversation.id), "status": "QUEUED"})
                return
            # 闲聊：纯文本回复落库，不创建任务。空回复按失败处理（与任务链路 runtime 的空回复防呆一致），
            # 避免落库空消息让用户看到"只有时间戳的空气泡"
            reply = (final_response.content or "").strip()
            if knowledge_chunks:
                reply = knowledge_service.ensure_answer_citations(reply, knowledge_chunks)
            if not reply:
                audit_model_call(
                    "MODEL_CALL_FAILED",
                    "model stream returned empty response",
                    {"status": "failed", "error_code": "AI_MODEL_RESPONSE_INVALID", "error_message": "model returned an empty response"},
                )
                user_message.status = "FAILED"
                db.commit()
                yield ev("message.error", {"code": "AI_MODEL_RESPONSE_INVALID", "message": "model returned an empty response"})
                return
            audit_model_call(
                "MODEL_CALL_COMPLETED",
                "model call completed",
                {
                    "provider_request_id": final_response.provider_request_id,
                    "usage": normalize_usage(final_response.usage),
                    "model": request.model or "unknown",
                    "tool_call_count": 0,
                },
            )
            assistant = Message(tenant_id=tenant_id, conversation_id=conversation.id, sender_type="ASSISTANT", content=reply, content_parts=assistant_reasoning_parts(final_response.reasoning_content), status="COMPLETED")
            db.add(assistant)
            db.flush()
            user_message.status = "COMPLETED"
            conversation.last_message_at = datetime.now(timezone.utc)
            db.commit()
            yield ev("message.result", {"message_id": str(assistant.id), "content": reply})
            yield ev("message.done", {"status": "COMPLETED"})
            # 会话记忆维护：失败不阻断回复（在 done 之后执行，避免延迟收尾事件）
            await _maybe_summarize(db, conversation=conversation, user_id=user_id, trace_id=trace_id or str(user_message_id), model_gateway=gateway, user_message_id=str(user_message_id), short_memory=short_memory)
    except Exception:
        logger.exception("stream chat finalize failed for message %s", user_message_id)
        yield ev("message.error", {"code": "AI_CHAT_FINALIZE_FAILED", "message": "chat finalize failed"})
