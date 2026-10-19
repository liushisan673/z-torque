import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from app.db.session import SessionLocal
from app.kernel.model_gateway import ModelGatewayError
from app.kernel.model_gateway.contracts import ModelRequest
from app.models import Conversation, Message
from app.services.chat_application import _maybe_summarize, create_chat_task

logger = logging.getLogger("z_torque.chat_stream")


def sse_event(event_id: str, event: str, data: dict[str, Any]) -> str:
    return f"id: {event_id}\nevent: {event}\ndata: {json.dumps(data, ensure_ascii=True, default=str)}\n\n"


async def stream_chat(*, gateway, request: ModelRequest, tenant_id: Any, user_id: Any, conversation_id: Any, user_message_id: str, idempotency_key: str | None, trace_id: str | None) -> AsyncIterator[str]:
    """Chat 流式统一入口（阶段05 2.6 分流）：

    - 流式期间透传 message.delta；
    - 结束后按模型返回分流：纯文本=闲聊落库（message.result + message.done，不创建任务）；
      tool_calls=创建会话任务并入队（message.task_created，前端切换到任务事件流）；
    - 模型错误：message.error 并把触发消息标记 FAILED。
    """
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
                user_message = db.get(Message, user_message_id)
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
            user_message = db.get(Message, user_message_id)
            conversation = db.get(Conversation, conversation_id)
            if user_message is None or conversation is None:
                yield ev("message.error", {"code": "AI_RESOURCE_NOT_FOUND", "message": "conversation or message not found"})
                return
            if final_response.tool_calls:
                # 分流：模型要求调用工具 → 创建会话任务并入队（Outbox TASK_READY），执行由 worker 完成
                task = create_chat_task(db, tenant_id=tenant_id, user_id=user_id, conversation=conversation, user_message=user_message, idempotency_key=idempotency_key or str(user_message_id), trace_id=trace_id)
                conversation.last_message_at = datetime.now(timezone.utc)
                db.commit()
                yield ev("message.task_created", {"task_id": str(task.id), "conversation_id": str(conversation.id), "status": "QUEUED"})
                return
            # 闲聊：纯文本回复落库，不创建任务
            reply = final_response.content or ""
            assistant = Message(tenant_id=tenant_id, conversation_id=conversation.id, sender_type="ASSISTANT", content=reply, status="COMPLETED")
            db.add(assistant)
            db.flush()
            user_message.status = "COMPLETED"
            conversation.last_message_at = datetime.now(timezone.utc)
            db.commit()
            yield ev("message.result", {"message_id": str(assistant.id), "content": reply})
            yield ev("message.done", {"status": "COMPLETED"})
            # 会话记忆维护：失败不阻断回复（在 done 之后执行，避免延迟收尾事件）
            await _maybe_summarize(db, conversation=conversation, user_id=user_id, trace_id=trace_id or str(user_message_id), model_gateway=gateway, user_message_id=str(user_message_id))
    except Exception:
        logger.exception("stream chat finalize failed for message %s", user_message_id)
        yield ev("message.error", {"code": "AI_CHAT_FINALIZE_FAILED", "message": "chat finalize failed"})
