import base64
import json
import time
import logging
from dataclasses import asdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import ApiError
from app.kernel.audit.service import record_audit
from app.kernel.model_gateway import ModelContentPart, ModelMessage
from app.kernel.registry.active import call_optional, try_port
from app.models import (
    AgentVersion,
    ContextSummary,
    Conversation,
    File,
    Message,
    ShortTermMemory,
    Skill,
)
from app.services.context_governance import (
    context_tokens,
    extractive_summary,
    heal_context,
    history_hash,
    resolve_chars_per_token,
    sanitize_untrusted,
    split_accepted_delta,
    stable_prefix,
    tool_pairs_balanced,
)
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
    from app.services.tool_mapping_builder import _dependency_ordered_links

    links = _dependency_ordered_links(links)
    link_labels = {}
    for link in links:
        version = db.get(ToolVersion, link.tool_version_id)
        parent = db.get(Tool, version.tool_id) if version else None
        link_labels[str(link.tool_version_id)] = parent.name if parent else str(link.tool_version_id)
    for index, link in enumerate(links[:30], start=1):
        tool_version = db.get(ToolVersion, link.tool_version_id)
        tool = db.get(Tool, tool_version.tool_id) if tool_version else None
        label = tool.name if tool else str(link.tool_version_id)
        suffix = []
        if link.role:
            suffix.append(f"角色：{link.role}")
        if link.depends_on:
            deps = [link_labels.get(str(dep), str(dep)[:8]) for dep in link.depends_on[:6]]
            suffix.append(f"依赖：{'、'.join(deps)}")
        if link.selector:
            criteria = []
            for key, value in list(link.selector.items())[:3]:
                text = "、".join(str(item) for item in value[:3]) if isinstance(value, list) else str(value)
                criteria.append(f"{key}={text[:60]}")
            suffix.append(f"选择条件：{'；'.join(criteria)}")
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
    from app.models import Agent, AgentVersionSkill, SkillVersion

    settings = get_settings()
    snapshot = agent_version.config_snapshot or {}
    system_prompt = snapshot.get("system_prompt")
    scopes = snapshot.get("skill_inject_scopes") or ["AGENT"]
    if skill_version_ids is None:
        skill_version_ids = [link.skill_version_id for link in db.query(AgentVersionSkill).filter(AgentVersionSkill.agent_version_id == agent_version.id).all()]
    candidates: list[tuple[int, str, UUID]] = []
    seen: set[UUID] = set()
    for sid in skill_version_ids:
        try:
            key = UUID(str(sid))
        except (ValueError, AttributeError, TypeError):
            continue
        if key not in seen:
            seen.add(key)
            candidates.append((2, "AGENT", key))

    def _add_published(level: int, label: str, rows) -> None:
        for parent in rows:
            version_id = getattr(parent, "current_published_version_id", None)
            if version_id is None or parent.status != "ACTIVE":
                continue
            key = UUID(str(version_id))
            if key in seen:
                continue
            seen.add(key)
            candidates.append((level, label, key))

    if "WORKSPACE" in scopes:
        _add_published(0, "WORKSPACE", db.scalars(select(Skill).where(Skill.tenant_id == agent_version.tenant_id, Skill.official.is_(True))).all())
    if "USER" in scopes:
        agent = db.get(Agent, agent_version.agent_id)
        if agent is not None and agent.owner_user_id is not None:
            _add_published(1, "USER", db.scalars(select(Skill).where(Skill.tenant_id == agent_version.tenant_id, Skill.owner_user_id == agent.owner_user_id, Skill.source_type == "USER")).all())

    candidates.sort(key=lambda item: (item[0], str(item[2])))
    budget = max(0, settings.skill_inject_max_chars)
    used = 0
    blocks: list[str] = []
    for _level, label, key in candidates[: max(0, settings.skill_inject_max_items)]:
        sv = db.get(SkillVersion, key)
        if sv is None or sv.status != "PUBLISHED":
            continue
        pieces: list[str] = []
        parent = db.get(Skill, sv.skill_id)
        if parent is not None and (parent.description or "").strip():
            pieces.append(f"[技能背景/{label}] {parent.name}：{parent.description.strip()[:200]}")
        if sv.instructions and sv.instructions.strip():
            pieces.append(sv.instructions.strip())
        orchestration = _skill_orchestration_text(db, sv)
        if orchestration:
            pieces.append(orchestration)
        text = "\n".join(pieces)
        if not text:
            continue
        remaining = budget - used
        if remaining <= 0:
            blocks.append("[技能预算] 已达上限，后续技能注入被截断。")
            break
        if len(text) > remaining:
            text = text[:remaining] + "…[技能预算截断]"
        blocks.append(text)
        used += len(text)
    if blocks:
        system_prompt = (system_prompt + "\n\n" if system_prompt else "") + "\n\n".join(blocks)
    # §2.1 强制引用：使用本地检索片段/文件附件回答时，必须标注可回查坐标。
    # 校验侧见 services/citation_guard.py；不在提示词里靠"请不要编造"软约束。
    citation_rule = "[引用约束] 使用检索片段或文件附件回答时，每个事实必须标注 [chunk:<chunk_id>] 或 [source:<path>#page=<n>/sheet=<s>#row=<r>#col=<c>]；未标注引用将被系统判定为未通过校验。"
    system_prompt = (system_prompt + "\n\n" if system_prompt else "") + citation_rule
    # 站内 A2A：系统智能体（用户设为默认，或未设置时的默认父）必须知道可把需求派发给专业智能体。
    from app.models import Agent as _Agent

    agent = db.get(_Agent, agent_version.agent_id)
    if agent is not None and agent.is_default_parent:
        dispatch_rule = (
            "[智能体协作与路由规则] 你是系统级 Supervisor，负责判断需求、选择专业 Agent、汇总结果；你本身不是所有领域的执行专家。"
            "先判断当前请求是否确实需要专业 Agent：闲聊、简单解释、确认、催促以及你可直接完成的通用问题，直接回答，不得为了调用工具而调用工具。"
            "需要专业能力时，使用当前工具列表中唯一的结构化 Agent 路由工具，从服务端提供的候选 Agent 中选择最匹配的一位；不得自行编造 Agent ID，也不得直接调用其他 Agent。"
            "派发前必须把用户目标、必要上下文、约束条件和期望输出格式整理成完整任务描述，不能只传一句模糊摘要。"
            "本版本一次请求仅允许一次站内 A2A 派发；不要在同一轮或后续轮次重复调用路由工具。"
            "没有匹配 Agent 时，应直接说明能力边界或向用户澄清，不得调用无关 Agent；不得虚构已调用或执行结果。"
            "收到 Agent 返回后，必须依据其结构化 status 判断成功或失败：status=FAILED 或 SKIPPED 时不得将其描述为成功完成；只总结返回内容，不得编造细节。"
        )
        system_prompt = (system_prompt + "\n\n") + dispatch_rule
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


def _context_fingerprint(conversation: Conversation, messages: list[Message], memory: ShortTermMemory | None, system_prompt: str | None, limit: int, attachment_ids: list | None, supports_vision: bool, provider: str | None, model: str | None, compaction_seq: int) -> str:
    """工作记忆版本号：任一输入变化（新消息/摘要更新/提示词或附件/provider/model/压缩序号变化）都会失效。

    cache key 含 provider/model/compaction_seq（路线图 v2.0 §2.4-7），避免跨模型复用错位前缀。
    """
    last_message_id = messages[0].id if messages else "-"
    memory_version = memory.updated_at.isoformat() if memory is not None and memory.updated_at else "-"
    return f"{conversation.id}|{last_message_id}|{len(messages)}|{memory_version}|{limit}|{system_prompt or ''}|{attachment_ids or ''}|{supports_vision}|{provider or ''}|{model or ''}|{compaction_seq}"


def _context_chars(context: list[ModelMessage]) -> int:
    return sum(len(item.content or "") for item in context)


def _enforce_hard_cap(context: list[ModelMessage], max_chars: int) -> list[ModelMessage]:
    """max_chars 硬上限二次保护：保留全部 system 前缀 + 尽量多的最新消息。

    仅在压缩后仍超限时触发；按时间倒序保留最新消息，避免把当前轮增量裁掉。
    """
    if _context_chars(context) <= max_chars:
        return context
    systems = [item for item in context if item.role == "system"]
    others = [item for item in context if item.role != "system"]
    used = _context_chars(systems)
    kept: list[ModelMessage] = []
    for item in reversed(others):
        size = len(item.content or "")
        if used + size > max_chars:
            continue
        kept.append(item)
        used += size
    kept.reverse()
    return systems + kept


def _compact_if_needed(
    db: Session,
    conversation: Conversation,
    context: list[ModelMessage],
    *,
    max_tokens: int,
    chars_per_token: float,
    max_chars: int,
    compaction_seq: int,
) -> tuple[list[ModelMessage], int]:
    """超预算时只压缩「模型已接受的历史前缀 H」，保留未发送增量 delta。

    成功：写 zr_context_summary 新版本，返回 [summary] + delta；
    失败：保留原历史 + audit `CONTEXT_COMPACTION_FAILED`，绝不写坏 summary 覆盖原历史。
    """
    budget_chars = min(max_chars, int(max_tokens * chars_per_token))
    if context_tokens(context, chars_per_token) <= max_tokens and _context_chars(context) <= budget_chars:
        return context, compaction_seq
    accepted, delta = split_accepted_delta(context)
    if not accepted:
        return context, compaction_seq
    try:
        summary_text = extractive_summary(accepted)
        next_seq = compaction_seq + 1
        db.add(ContextSummary(
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            compaction_seq=next_seq,
            summary=summary_text,
            accepted_history_hash=history_hash(accepted),
            status="ACTIVE",
        ))
        db.flush()
        summary_message = ModelMessage(
            "system",
            f"Conversation summary (accepted history, compaction seq={next_seq}):\n{summary_text}",
        )
        return [summary_message, *delta], next_seq
    except Exception as exc:
        record_audit(
            db,
            tenant_id=conversation.tenant_id,
            action="CONTEXT_COMPACTION_FAILED",
            resource_type="CONVERSATION",
            resource_id=conversation.id,
            actor_user_id=conversation.owner_user_id,
            summary="context compaction failed; original history preserved",
            detail={"error": str(exc), "compaction_seq": compaction_seq},
        )
        logger.warning("context compaction failed for conversation %s: %s", conversation.id, exc, exc_info=True)
        return context, compaction_seq


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


def _format_knowledge_chunks(chunks: list[Any], *, max_chars: int = 4000) -> str:
    lines: list[str] = []
    used = 0
    for chunk in chunks:
        metadata = chunk.metadata or {}
        chunk_id = metadata.get("chunk_id")
        if not chunk_id:
            continue
        if metadata.get("page_no") is not None:
            coord = f"page={metadata['page_no']}"
        elif metadata.get("sheet") is not None and metadata.get("row_no") is not None:
            coord = f"sheet={metadata['sheet']}#row={metadata['row_no']}#col={metadata.get('col_no')}"
        else:
            coord = f"line={metadata.get('line_start')}-{metadata.get('line_end')}"
        line = f"[chunk:{chunk_id}] ({coord}) {chunk.content[:500]}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _knowledge_excerpt(
    db: Session,
    item: File,
    query: str,
    *,
    tenant_id: UUID,
    owner_user_id: UUID,
    max_chars: int = 4000,
) -> str:
    """通过统一 KnowledgeProvider 检索单个附件，避免按 seq 扫描 200 个 chunk。"""
    provider = try_port("knowledge.provider")
    if provider is None:
        return f"Attachment {item.original_name}: [knowledge search unavailable]"
    started = time.perf_counter()
    try:
        chunks = provider.search(
            db=db,
            tenant_id=tenant_id,
            query=query,
            top_k=5,
            filters={"owner_user_id": str(owner_user_id), "file_id": str(item.id)},
        )
        emit_event("knowledge.retrieval", {
            "scope": "attachment",
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "query_chars": len(query),
            "result_count": len(chunks),
        })
    except Exception:  # noqa: BLE001 - 检索降级不阻断普通对话
        emit_event("knowledge.retrieval", {
            "scope": "attachment",
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "query_chars": len(query),
            "result_count": 0,
            "outcome": "failed",
        })
        logger.warning("attachment knowledge retrieval failed file_id=%s", item.id, exc_info=True)
        return f"Attachment {item.original_name}: [knowledge search unavailable]"
    excerpt = _format_knowledge_chunks(list(chunks), max_chars=max_chars)
    return excerpt or f"Attachment {item.original_name}: [no matching chunk]"


def _global_knowledge_context(
    db: Session,
    conversation: Conversation,
    query: str,
    *,
    exclude_file_ids: list[str],
    max_chars: int = 4000,
) -> str:
    """检索当前用户拥有的全部已解析文档，而不局限于本轮显式附件。"""
    if not query.strip():
        return ""
    provider = try_port("knowledge.provider")
    if provider is None:
        return ""
    started = time.perf_counter()
    try:
        chunks = provider.search(
            db=db,
            tenant_id=conversation.tenant_id,
            query=query,
            top_k=6,
            filters={
                "owner_user_id": str(conversation.owner_user_id),
                "exclude_file_ids": exclude_file_ids,
            },
        )
        emit_event("knowledge.retrieval", {
            "scope": "user_library",
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "query_chars": len(query),
            "result_count": len(chunks),
        })
    except Exception:  # noqa: BLE001 - 知识检索失败应降级为无知识上下文，不阻断聊天
        emit_event("knowledge.retrieval", {
            "scope": "user_library",
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "query_chars": len(query),
            "result_count": 0,
            "outcome": "failed",
        })
        logger.warning("global knowledge retrieval failed conversation_id=%s", conversation.id, exc_info=True)
        return ""
    return _format_knowledge_chunks(list(chunks), max_chars=max_chars)


def build_context(
    db: Session,
    conversation: Conversation,
    *,
    system_prompt: str | None = None,
    limit: int | None = None,
    max_chars: int = 48000,
    attachment_ids: list | None = None,
    supports_vision: bool = False,
    supports_audio: bool = False,
    provider: str | None = None,
    model: str | None = None,
    trace_id: str | None = None,
) -> list[ModelMessage]:
    settings = get_settings()
    rounds = settings.context_recent_rounds if limit is None else limit
    # 多取一倍窗口，使超预算时 H/delta 压缩有历史可压（不静默丢弃整条）。
    load_limit = max(rounds * 2, 20)
    summary_row = db.scalar(
        select(ContextSummary)
        .where(ContextSummary.tenant_id == conversation.tenant_id, ContextSummary.conversation_id == conversation.id, ContextSummary.status == "ACTIVE")
        .order_by(ContextSummary.compaction_seq.desc())
    )
    compaction_seq = summary_row.compaction_seq if summary_row is not None else 0
    messages = db.scalars(select(Message).where(Message.tenant_id == conversation.tenant_id, Message.conversation_id == conversation.id).order_by(Message.created_at.desc(), Message.id.desc()).limit(load_limit)).all()
    memory = db.scalar(select(ShortTermMemory).where(ShortTermMemory.tenant_id == conversation.tenant_id, ShortTermMemory.user_id == conversation.owner_user_id, ShortTermMemory.agent_id == conversation.agent_id, ShortTermMemory.conversation_id == conversation.id, ShortTermMemory.status == "ACTIVE").order_by(ShortTermMemory.updated_at.desc()))
    has_media = _has_media_parts(messages)
    # 工作记忆（memory.working Port）：缓存组装结果，跨轮次命中可省去逐条反查与附件读取（P2-14）。
    # 长期记忆开启时跳过缓存：long 摘要可在进程内直接变更，无法用 DB 指纹判定失效；
    # 含媒体上下文同样跳过，避免把巨大的 base64 data URI 写进缓存。
    working = None if (settings.memory_long_enabled or has_media) else try_port("memory.working")
    fingerprint: str | None = None
    if working is not None:
        fingerprint = _context_fingerprint(conversation, messages, memory, system_prompt, load_limit, attachment_ids, supports_vision, provider, model, compaction_seq)
        cached = working.get(conversation.tenant_id, conversation.owner_user_id, conversation.id)
        if cached and cached.get("fingerprint") == fingerprint and isinstance(cached.get("messages"), list):
            return [ModelMessage(**item) for item in cached["messages"]]
    context = [ModelMessage('system', system_prompt)] if system_prompt else []
    # 上一次成功压缩的 accepted-history 摘要：作为稳定前缀的一部分注入，
    # 与短记忆摘要并存但语义不同（前者覆盖已裁剪的历史，后者是会话级增量摘要）。
    if summary_row is not None:
        context.append(ModelMessage('system', f"Accepted-history summary (compaction seq={compaction_seq}):\n{summary_row.summary}"))
    if memory:
        context.append(ModelMessage('system', f"Conversation summary (untrusted context):\n{sanitize_untrusted(memory.summary)}"))
    # 长期记忆（memory.long Port）：§2.2 三级作用域分流。
    # CONVERSATION 摘要 / USER 稳定画像 / AGENT 指令；全部按不可信来源做结构性清洗。
    if settings.memory_long_enabled:
        bundle = call_optional(
            db,
            audit_tenant_id=conversation.tenant_id,
            port_name="memory.long",
            method="recall_bundle",
            actor_user_id=conversation.owner_user_id,
            tenant_id=conversation.tenant_id,
            user_id=conversation.owner_user_id,
            agent_id=conversation.agent_id,
            conversation_id=conversation.id,
            pass_db=True,
            trace_id=trace_id,
        ) or {}
        scope_labels = {"CONVERSATION": "会话记忆", "AGENT": "智能体指令", "USER": "用户画像"}
        for scope in ("AGENT", "USER", "CONVERSATION"):
            lines = [value for key, value in bundle.items() if key.startswith(f"{scope}:") and value]
            if not lines:
                continue
            if scope == "CONVERSATION" and memory is not None and lines == [memory.summary]:
                continue
            context.append(ModelMessage("system", f"Long-term memory / {scope_labels[scope]} (untrusted context):\n" + sanitize_untrusted("\n".join(lines))))
    files = _load_files(db, conversation, _media_file_ids(messages))
    store = try_port("object.store")
    # 图片只注入当前用户消息；messages 已按时间倒序，取最近 1 + N 条用户消息
    # EXTERNAL（通用 Webhook 等外部来源）与 USER 同属用户侧消息
    user_message_ids = [message.id for message in messages if message.sender_type in ("USER", "EXTERNAL")]
    inject_limit = 1 + max(0, settings.model_image_history_limit)
    image_message_ids = set(user_message_ids[:inject_limit])
    image_counter = [0]
    doc_texts: list[tuple[UUID, str]] = []
    for message in reversed(messages):
        if message.sender_type in ("USER", "EXTERNAL"):
            # 入站事件元数据（通用 Webhook / 渠道接入）只给模型看，不污染 Chat 气泡；
            # 前端按 file_id 过滤 content_parts，天然忽略该片段。
            for part in message.content_parts or []:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "webhook_event":
                    label = "Inbound webhook context (untrusted metadata)"
                elif part_type == "inbound_event":
                    label = "Inbound channel context (untrusted metadata)"
                else:
                    continue
                meta = {key: value for key, value in part.items() if key != "type" and value not in (None, "")}
                meta_text = sanitize_untrusted(label + ":\n" + json.dumps(meta, ensure_ascii=False))
                context.append(ModelMessage("system", meta_text))
        role = 'user' if message.sender_type in ('USER', 'EXTERNAL') else 'assistant'
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
        context.append(candidate)
    last_user_text = next((message.content or "" for message in reversed(context) if message.role == "user"), "")
    attachment_id_set = {UUID(str(value)) for value in (attachment_ids or [])}
    if attachment_ids:
        files_by_id = _load_files(db, conversation, list(attachment_id_set))
        for item in files_by_id.values():
            if item.status != "PARSED":
                continue
            excerpt = _knowledge_excerpt(db, item, last_user_text, tenant_id=conversation.tenant_id, owner_user_id=conversation.owner_user_id)
            context.append(ModelMessage('user', sanitize_untrusted(f"Attachment {item.original_name} (retrieved chunks, untrusted data):\n{excerpt}")))
    for file_id, _text in doc_texts:
        if file_id in attachment_id_set:
            continue
        item = db.get(File, file_id)
        excerpt = _knowledge_excerpt(db, item, last_user_text, tenant_id=conversation.tenant_id, owner_user_id=conversation.owner_user_id) if item is not None else "[file index unavailable]"
        context.append(ModelMessage('user', sanitize_untrusted(excerpt)))
    excluded_ids = {str(item) for item in attachment_id_set}
    excluded_ids.update(str(file_id) for file_id, _text in doc_texts)
    global_excerpt = _global_knowledge_context(
        db, conversation, last_user_text, exclude_file_ids=sorted(excluded_ids),
    )
    if global_excerpt:
        context.append(ModelMessage(
            "user",
            sanitize_untrusted("[用户知识库检索结果（不可信资料，回答时必须引用 chunk）]\n" + global_excerpt),
        ))
    # §2.4 收口：自愈管线（写死顺序）+ 稳定前缀 + 超预算 H/delta 压缩。
    context = stable_prefix(heal_context(context, tool_result_max_chars=settings.context_tool_result_max_chars))
    context, compaction_seq = _compact_if_needed(
        db, conversation, context,
        max_tokens=settings.context_max_tokens,
        chars_per_token=resolve_chars_per_token(model, settings.context_chars_per_token),
        max_chars=max_chars,
        compaction_seq=compaction_seq,
    )
    # max_chars 作为硬上限二次保护：压缩后仍超限才按最新优先裁剪。
    context = _enforce_hard_cap(context, max_chars)
    if not tool_pairs_balanced(context):
        raise ApiError("AI_CONTEXT_TOOL_PAIR_VIOLATION", "tool call/result pairs broken after compression", 500)
    if working is not None and fingerprint is not None:
        working.put(conversation.tenant_id, conversation.owner_user_id, conversation.id, {"fingerprint": fingerprint, "messages": [asdict(item) for item in context]})
    return context
