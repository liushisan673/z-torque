import base64
import json
import logging
from dataclasses import asdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.kernel.model_gateway import ModelContentPart, ModelMessage
from app.kernel.registry.active import call_optional, try_port
from app.models import AgentVersion, Conversation, File, Message, ShortTermMemory, Skill
from app.services.media_ingest import (
    MEDIA_KIND_AUDIO,
    MEDIA_KIND_IMAGE,
    MEDIA_KIND_VIDEO,
    media_kind_of,
)

logger = logging.getLogger("z_torque.context_builder")


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


def _context_fingerprint(conversation: Conversation, messages: list[Message], memory: ShortTermMemory | None, system_prompt: str | None, limit: int, attachment_ids: list | None, supports_vision: bool) -> str:
    """工作记忆版本号：任一输入变化（新消息/摘要更新/提示词或附件变化）都会失效。"""
    last_message_id = messages[0].id if messages else "-"
    memory_version = memory.updated_at.isoformat() if memory is not None and memory.updated_at else "-"
    return f"{conversation.id}|{last_message_id}|{len(messages)}|{memory_version}|{limit}|{system_prompt or ''}|{attachment_ids or ''}|{supports_vision}"


def _context_chars(context: list[ModelMessage]) -> int:
    return sum(len(item.content or "") for item in context)


def _has_media_parts(messages: list[Message]) -> bool:
    """是否含图片/音频/视频片段；含媒体上下文必须跳过 working 缓存。"""
    for message in messages:
        for part in message.content_parts or []:
            if isinstance(part, dict) and part.get("type") in {MEDIA_KIND_IMAGE, MEDIA_KIND_AUDIO, MEDIA_KIND_VIDEO}:
                return True
    return False


def _media_file_ids(messages: list[Message]) -> list[UUID]:
    ids: list[UUID] = []
    for message in messages:
        for part in message.content_parts or []:
            if not isinstance(part, dict) or not part.get("file_id"):
                continue
            try:
                ids.append(UUID(str(part["file_id"])))
            except ValueError:
                continue
    return list(dict.fromkeys(ids))


def _load_files(db: Session, conversation: Conversation, ids: list[UUID]) -> dict[UUID, File]:
    if not ids:
        return {}
    rows = db.scalars(
        select(File).where(
            File.id.in_(ids),
            File.tenant_id == conversation.tenant_id,
            File.owner_user_id == conversation.owner_user_id,
            File.conversation_id == conversation.id,
            File.status != "DELETED",
        )
    ).all()
    return {row.id: row for row in rows}


def _image_data_uri(store: Any, item: File, max_bytes: int) -> str | None:
    if item.size_bytes > max_bytes:
        return None
    try:
        raw = store.local_path(item.storage_key).read_bytes()
    except Exception:  # pragma: no cover - 存储异常按不可读降级
        logger.warning("failed to read image object file_id=%s", item.id, exc_info=True)
        return None
    mime = item.media_type if (item.media_type or "").startswith("image/") else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _media_content(
    message: Message,
    files: dict[UUID, File],
    *,
    supports_vision: bool,
    include_images: bool,
    store: Any,
    settings: Any,
    image_counter: list[int],
) -> tuple[list[ModelContentPart], list[str], list[tuple[UUID, str]]]:
    """返回 (图片片段, 降级文本提示, 文档文本及其 file_id)。"""
    image_parts: list[ModelContentPart] = []
    hints: list[str] = []
    docs: list[tuple[UUID, str]] = []
    for part in message.content_parts or []:
        if not isinstance(part, dict) or not part.get("file_id"):
            continue
        try:
            item = files.get(UUID(str(part["file_id"])))
        except ValueError:
            continue
        if item is None:
            continue
        kind = media_kind_of(item)
        bridge_path = part.get("bridge_path")
        # 共享卷路径只对当前用户消息提示，避免历史消息反复注入
        forwardable = bool(bridge_path) and include_images
        if kind == MEDIA_KIND_IMAGE:
            if not include_images:
                continue
            name = item.original_name
            if not supports_vision:
                hints.append(f"[已接收图片: {name}]")
                if forwardable:
                    hints.append(f"[图片可转发: {name}, bridge_path={bridge_path}]")
                continue
            if image_counter[0] >= settings.model_max_images_per_message:
                hints.append(f"[图片数量超过上限，已跳过: {name}]")
                continue
            data_uri = _image_data_uri(store, item, settings.model_max_image_mb * 1024 * 1024) if store is not None else None
            if data_uri is None:
                hints.append(f"[图片过大或读取失败，已跳过: {name}]")
                continue
            image_parts.append(ModelContentPart("image_url", image_url=data_uri))
            image_counter[0] += 1
            if forwardable:
                hints.append(f"[图片可转发: {name}, bridge_path={bridge_path}]")
        elif kind == MEDIA_KIND_AUDIO:
            hint = f"[已接收音频: {item.original_name}]"
            if forwardable:
                hint += f" [可转发: bridge_path={bridge_path}]"
            hints.append(hint)
        elif kind == MEDIA_KIND_VIDEO:
            hint = f"[已接收视频: {item.original_name}]"
            if forwardable:
                hint += f" [可转发: bridge_path={bridge_path}]"
            hints.append(hint)
        elif item.status == "PARSED" and item.extracted_text:
            docs.append((item.id, f"Attachment {item.original_name} (untrusted data):\n{item.extracted_text}"))
            if forwardable:
                hints.append(f"[文件可转发: {item.original_name}, bridge_path={bridge_path}]")
        elif forwardable:
            hints.append(f"[已接收文件: {item.original_name}, bridge_path={bridge_path}]")
    return image_parts, hints, docs


def build_context(
    db: Session,
    conversation: Conversation,
    *,
    system_prompt: str | None = None,
    limit: int = 5,
    max_chars: int = 48000,
    attachment_ids: list | None = None,
    supports_vision: bool = False,
    supports_audio: bool = False,
) -> list[ModelMessage]:
    settings = get_settings()
    messages = db.scalars(select(Message).where(Message.tenant_id == conversation.tenant_id, Message.conversation_id == conversation.id).order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)).all()
    memory = db.scalar(select(ShortTermMemory).where(ShortTermMemory.tenant_id == conversation.tenant_id, ShortTermMemory.user_id == conversation.owner_user_id, ShortTermMemory.agent_id == conversation.agent_id, ShortTermMemory.conversation_id == conversation.id, ShortTermMemory.status == "ACTIVE").order_by(ShortTermMemory.updated_at.desc()))
    has_media = _has_media_parts(messages)
    # 工作记忆（memory.working Port）：缓存组装结果，跨轮次命中可省去逐条反查与附件读取（P2-14）。
    # 长期记忆开启时跳过缓存：long 摘要可在进程内直接变更，无法用 DB 指纹判定失效；
    # 含媒体上下文同样跳过，避免把巨大的 base64 data URI 写进缓存。
    working = None if (settings.memory_long_enabled or has_media) else try_port("memory.working")
    fingerprint: str | None = None
    if working is not None:
        fingerprint = _context_fingerprint(conversation, messages, memory, system_prompt, limit, attachment_ids, supports_vision)
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
    files = _load_files(db, conversation, _media_file_ids(messages))
    store = try_port("object.store")
    # 图片只注入当前用户消息；messages 已按时间倒序，取最近 1 + N 条用户消息
    user_message_ids = [message.id for message in messages if message.sender_type == "USER"]
    inject_limit = 1 + max(0, settings.model_image_history_limit)
    image_message_ids = set(user_message_ids[:inject_limit])
    image_counter = [0]
    doc_texts: list[tuple[UUID, str]] = []
    for message in reversed(messages):
        if message.sender_type == "USER":
            # 入站事件元数据（企微 / 通用 Webhook / 渠道接入）只给模型看，不污染 Chat 气泡；
            # 前端按 file_id 过滤 content_parts，天然忽略该片段。
            for part in message.content_parts or []:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "wecom_event":
                    label = "WeCom event context (trusted metadata)"
                elif part_type == "webhook_event":
                    label = "Inbound webhook context (untrusted metadata)"
                elif part_type == "inbound_event":
                    label = "Inbound channel context (untrusted metadata)"
                else:
                    continue
                meta = {key: value for key, value in part.items() if key != "type" and value not in (None, "")}
                meta_text = label + ":\n" + json.dumps(meta, ensure_ascii=False)
                if _context_chars(context) + len(meta_text) <= max_chars:
                    context.append(ModelMessage("system", meta_text))
        role = 'user' if message.sender_type == 'USER' else 'assistant'
        image_parts, hints, docs = _media_content(
            message,
            files,
            supports_vision=supports_vision,
            include_images=message.id in image_message_ids,
            store=store,
            settings=settings,
            image_counter=image_counter,
        )
        doc_texts.extend(docs)
        content = message.content or ""
        if hints:
            content = "\n".join([part for part in [content, *hints] if part])
        candidate = ModelMessage(role, content, content_parts=image_parts or None, reasoning_content=_reasoning_of(message))
        if _context_chars(context) + len(content) <= max_chars:
            context.append(candidate)
    attachment_id_set = {UUID(str(value)) for value in (attachment_ids or [])}
    if attachment_ids:
        files_by_id = _load_files(db, conversation, list(attachment_id_set))
        for item in files_by_id.values():
            if item.status != "PARSED":
                continue
            text = f"Attachment {item.original_name} (untrusted data):\n{item.extracted_text or ''}"
            if _context_chars(context) + len(text) <= max_chars:
                context.append(ModelMessage('user', text))
    for file_id, text in doc_texts:
        if file_id in attachment_id_set:
            continue
        if _context_chars(context) + len(text) <= max_chars:
            context.append(ModelMessage('user', text))
    if working is not None and fingerprint is not None:
        working.put(conversation.tenant_id, conversation.owner_user_id, conversation.id, {"fingerprint": fingerprint, "messages": [asdict(item) for item in context]})
    return context
