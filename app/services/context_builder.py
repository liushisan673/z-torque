from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel.model_gateway import ModelMessage
from app.models import AgentVersion, Conversation, File, Message, ShortTermMemory


def build_agent_system_prompt(db: Session, agent_version: AgentVersion | None, skill_version_ids: list[UUID] | None = None) -> str | None:
    """智能体系统提示 + 绑定 skill 的执行指令注入（任务链路与流式链路共用，避免两处逻辑漂移）。

    skill_version_ids 传入时按该集合注入（执行循环已有映射）；未传时按版本绑定关系自查（流式纯文本链路）。
    """
    if agent_version is None:
        return None
    system_prompt = (agent_version.config_snapshot or {}).get("system_prompt")
    if skill_version_ids is None:
        from app.models import AgentVersionSkill
        skill_version_ids = [link.skill_version_id for link in db.query(AgentVersionSkill).filter(AgentVersionSkill.agent_version_id == agent_version.id).all()]
    from app.models import SkillVersion
    instructions: list[str] = []
    seen: set[UUID] = set()
    for sid in skill_version_ids:
        key = UUID(str(sid))
        if key in seen:
            continue
        seen.add(key)
        sv = db.get(SkillVersion, key)
        if sv is not None and sv.instructions and sv.instructions.strip():
            instructions.append(sv.instructions.strip())
    if instructions:
        system_prompt = (system_prompt + "\n\n" if system_prompt else "") + "\n\n".join(instructions)
    return system_prompt


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
