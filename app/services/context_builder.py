from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel.model_gateway import ModelMessage
from app.models import Conversation, File, Message, ShortTermMemory


def build_context(db: Session, conversation: Conversation, *, system_prompt: str | None = None, limit: int = 5, max_chars: int = 48000, attachment_ids: list | None = None) -> list[ModelMessage]:
    messages = db.scalars(select(Message).where(Message.tenant_id == conversation.tenant_id, Message.conversation_id == conversation.id).order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)).all()
    context = [ModelMessage('system', system_prompt)] if system_prompt else []
    memory = db.scalar(select(ShortTermMemory).where(ShortTermMemory.tenant_id == conversation.tenant_id, ShortTermMemory.user_id == conversation.owner_user_id, ShortTermMemory.agent_id == conversation.agent_id, ShortTermMemory.conversation_id == conversation.id, ShortTermMemory.status == "ACTIVE").order_by(ShortTermMemory.updated_at.desc()))
    if memory:
        context.append(ModelMessage('system', f"Conversation summary (untrusted context):\n{memory.summary}"))
    for message in reversed(messages):
        role = 'user' if message.sender_type == 'USER' else 'assistant'
        candidate = ModelMessage(role, message.content)
        if sum(len(item.content) for item in context) + len(candidate.content) <= max_chars:
            context.append(candidate)
    if attachment_ids:
        files = db.scalars(select(File).where(File.id.in_(attachment_ids), File.tenant_id == conversation.tenant_id, File.owner_user_id == conversation.owner_user_id, File.conversation_id == conversation.id, File.status == "PARSED")).all()
        for item in files:
            text = f"Attachment {item.original_name} (untrusted data):\n{item.extracted_text or ''}"
            if sum(len(value.content or "") for value in context) + len(text) <= max_chars:
                context.append(ModelMessage('user', text))
    return context
