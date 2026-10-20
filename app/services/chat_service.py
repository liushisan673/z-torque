from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.kernel.security_context import SecurityContext
from app.kernel.task.service import create_task
from app.models import Agent, Conversation, Message


@dataclass(frozen=True)
class ChatResult:
    user_message: Message
    assistant_message: Message | None
    task_id: UUID | None = None
    mocked: bool = True
    # 写入类工具触发人工审批时的挂起信息（confirmation_id/tool_name/arguments/task_id）
    pending_confirmation: dict | None = None
    # 无任务直答（无工具智能体）时的回复文本：随 POST /chat 响应直接返回，供前端流式渲染
    response_content: str | None = None


def handle_mock_chat(db: Session, *, ctx: SecurityContext, conversation_id: UUID, content: str, requires_task: bool = False, idempotency_key: str | None = None, trace_id: str | None = None) -> ChatResult:
    tenant_id = ctx.tenant_id
    user_id = ctx.user_id
    trace_id = trace_id or ctx.trace_id
    conversation = db.scalar(select(Conversation).where(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id, Conversation.owner_user_id == user_id, Conversation.status == "ACTIVE"))
    if conversation is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "conversation not found", 404)
    user_message = Message(tenant_id=tenant_id, conversation_id=conversation.id, sender_type="USER", content=content, status="PROCESSING")
    db.add(user_message)
    db.flush()
    conversation.last_message_at = datetime.now(timezone.utc)
    if requires_task:
        if not idempotency_key:
            raise ApiError("AI_PARAM_INVALID", "idempotency key is required for task chat", 400)
        task = create_task(db, ctx=ctx, agent_id=conversation.agent_id, agent_version_id=_published_version_id(db, conversation.agent_id), idempotency_key=idempotency_key, request_summary=content, conversation_id=conversation.id, trigger_message_id=user_message.id, trace_id=trace_id)
        user_message.status = "COMPLETED"
        return ChatResult(user_message=user_message, assistant_message=None, task_id=task.id)
    assistant = Message(tenant_id=tenant_id, conversation_id=conversation.id, sender_type="ASSISTANT", content="[MOCK] " + content, status="COMPLETED")
    user_message.status = "COMPLETED"
    db.add(assistant)
    db.flush()
    conversation.last_message_at = datetime.now(timezone.utc)
    return ChatResult(user_message=user_message, assistant_message=assistant)


def _published_version_id(db: Session, agent_id: UUID) -> UUID:
    agent = db.scalar(select(Agent).where(Agent.id == agent_id))
    if agent is None or agent.current_published_version_id is None:
        raise ApiError("AI_AGENT_VERSION_NOT_PUBLISHED", "agent has no published version", 409)
    return agent.current_published_version_id