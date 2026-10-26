from dataclasses import asdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.kernel.model_gateway import ModelMessage
from app.kernel.registry.active import call_optional, try_port
from app.models import AgentVersion, Conversation, File, Message, ShortTermMemory, Skill


def _skill_orchestration_text(db: Session, skill_version) -> str | None:
    """生成单个 SkillVersion 的工具顺序/角色/备注与调用提示（发布后固化，长度受限）。"""
    from app.models import SkillVersionTool, Tool, ToolVersion

    lines: list[str] = []
    links = (
        db.query(SkillVersionTool)
        .filter(SkillVersionTool.skill_version_id == skill_version.id)
        .order_by(SkillVersionTool.sort_order, SkillVersionTool.created_at)
        .all()
    )
    for index, link in enumerate(links[:30], start=1):
        tool_version = db.get(ToolVersion, link.tool_version_id)
        tool = db.get(Tool, tool_version.tool_id) if tool_version else None
        label = tool.name if tool else str(link.tool_version_id)
        suffix = []
        if link.role:
            suffix.append(f"角色：{link.role}")
        if link.note:
            suffix.append(f"说明：{link.note.strip()[:120]}")
        lines.append(f"{index}. {label}" + (f"（{'；'.join(suffix)}）" if suffix else ""))
    blocks: list[str] = []
    if lines:
        blocks.append("[工具调用顺序]\n" + "\n".join(lines))
    hint = (skill_version.orchestration_hint or "").strip()
    if hint:
        blocks.append("[调用提示]\n" + hint[:2000])
    return "\n\n".join(blocks) or None


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
        if sv is None:
            continue
        # 技能描述背景：description 此前仅作列表展示，不进模型上下文；此处注入一句概述
        # （截断 200 字防提示膨胀），让模型理解各绑定技能的用途边界
        parent = db.get(Skill, sv.skill_id)
        if parent is not None and (parent.description or "").strip():
            instructions.append(f"[技能背景] {parent.name}：{parent.description.strip()[:200]}")
        if sv.instructions and sv.instructions.strip():
            instructions.append(sv.instructions.strip())
        orchestration = _skill_orchestration_text(db, sv)
        if orchestration:
            instructions.append(orchestration)
    if instructions:
        system_prompt = (system_prompt + "\n\n" if system_prompt else "") + "\n\n".join(instructions)
    return system_prompt


def assistant_reasoning_parts(reasoning_content: str | None) -> list[dict[str, Any]] | None:
    """assistant 回复的思考过程落库载体（content_parts 复用）：None 表示无思考内容。"""
    if not reasoning_content:
        return None
    return [{"type": "reasoning", "text": reasoning_content}]


def _reasoning_of(message: Message) -> str | None:
    for part in (message.content_parts or []):
        if isinstance(part, dict) and part.get("type") == "reasoning":
            return str(part.get("text") or "") or None
    return None


def _long_memory_key(conversation: Conversation) -> str:
    return f"conversation:{conversation.id}:summary"


def _context_fingerprint(conversation: Conversation, messages: list[Message], memory: ShortTermMemory | None, system_prompt: str | None, limit: int, attachment_ids: list | None) -> str:
    """工作记忆版本号：任一输入变化（新消息/摘要更新/提示词或附件变化）都会失效。"""
    last_message_id = messages[0].id if messages else "-"
    memory_version = memory.updated_at.isoformat() if memory is not None and memory.updated_at else "-"
    return f"{conversation.id}|{last_message_id}|{len(messages)}|{memory_version}|{limit}|{system_prompt or ''}|{attachment_ids or ''}"


def build_context(db: Session, conversation: Conversation, *, system_prompt: str | None = None, limit: int = 5, max_chars: int = 48000, attachment_ids: list | None = None) -> list[ModelMessage]:
    settings = get_settings()
    messages = db.scalars(select(Message).where(Message.tenant_id == conversation.tenant_id, Message.conversation_id == conversation.id).order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)).all()
    memory = db.scalar(select(ShortTermMemory).where(ShortTermMemory.tenant_id == conversation.tenant_id, ShortTermMemory.user_id == conversation.owner_user_id, ShortTermMemory.agent_id == conversation.agent_id, ShortTermMemory.conversation_id == conversation.id, ShortTermMemory.status == "ACTIVE").order_by(ShortTermMemory.updated_at.desc()))
    # 工作记忆（memory.working Port）：缓存组装结果，跨轮次命中可省去逐条反查与附件读取（P2-14）。
    # 长期记忆开启时跳过缓存：long 摘要可在进程内直接变更，无法用 DB 指纹判定失效。
    working = None if settings.memory_long_enabled else try_port("memory.working")
    fingerprint: str | None = None
    if working is not None:
        fingerprint = _context_fingerprint(conversation, messages, memory, system_prompt, limit, attachment_ids)
        cached = working.get(conversation.tenant_id, conversation.owner_user_id, conversation.id)
        if cached and cached.get("fingerprint") == fingerprint and isinstance(cached.get("messages"), list):
            return [ModelMessage(**item) for item in cached["messages"]]
    context = [ModelMessage('system', system_prompt)] if system_prompt else []
    if memory:
        context.append(ModelMessage('system', f"Conversation summary (untrusted context):\n{memory.summary}"))
    # 长期记忆（memory.long Port）：仅在 memory_long_enabled 时召回，且不重复注入当前摘要
    if settings.memory_long_enabled:
        long_summary = call_optional(
            db,
            audit_tenant_id=conversation.tenant_id,
            port_name="memory.long",
            method="recall",
            actor_user_id=conversation.owner_user_id,
            tenant_id=conversation.tenant_id,
            user_id=conversation.owner_user_id,
            key=_long_memory_key(conversation),
        )
        if long_summary and (memory is None or long_summary != memory.summary):
            context.append(ModelMessage('system', f"Conversation summary from long-term memory (untrusted context):\n{long_summary}"))
    for message in reversed(messages):
        role = 'user' if message.sender_type == 'USER' else 'assistant'
        candidate = ModelMessage(role, message.content, reasoning_content=_reasoning_of(message))
        if sum(len(item.content) for item in context) + len(candidate.content) <= max_chars:
            context.append(candidate)
    if attachment_ids:
        files = db.scalars(select(File).where(File.id.in_(attachment_ids), File.tenant_id == conversation.tenant_id, File.owner_user_id == conversation.owner_user_id, File.conversation_id == conversation.id, File.status == "PARSED")).all()
        for item in files:
            text = f"Attachment {item.original_name} (untrusted data):\n{item.extracted_text or ''}"
            if sum(len(value.content or "") for value in context) + len(text) <= max_chars:
                context.append(ModelMessage('user', text))
    if working is not None and fingerprint is not None:
        working.put(conversation.tenant_id, conversation.owner_user_id, conversation.id, {"fingerprint": fingerprint, "messages": [asdict(item) for item in context]})
    return context
