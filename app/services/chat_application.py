import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import ApiError
from app.kernel.model_gateway import ModelGateway, ModelGatewayError, ModelRequest
from app.kernel.ports.memory_short import ShortTermMemoryProvider
from app.kernel.ports.model_selector import snapshot_model_params
from app.kernel.security_context import SecurityContext
from app.kernel.skill_gateway import SkillGateway
from app.kernel.task.service import create_task
from app.models import Agent, AgentVersion, Conversation, Message, Task
from app.models.message import message_file
from app.security.permissions import admin_owned_clause
from app.services.chat_service import ChatResult
from app.services.context_builder import (
    assistant_reasoning_parts,
    build_agent_system_prompt,
    build_context,
)
from app.services.model_limits import effective_max_tokens
from app.services.tool_mapping_builder import build_tool_mappings

logger = logging.getLogger("z_torque.chat")


def create_chat_task(db: Session, *, ctx: SecurityContext, conversation: Conversation, user_message: Message, idempotency_key: str, trace_id: str | None) -> Task:
    """会话任务创建（POST /chat 任务链路与流式分流共用）：绑定会话当前发布版本。"""
    agent_version = _published_agent_version(db, conversation.agent_id)
    return create_task(
        db,
        ctx=ctx,
        agent_id=conversation.agent_id,
        agent_version_id=agent_version.id,
        idempotency_key=idempotency_key,
        request_summary=user_message.content or "",
        conversation_id=conversation.id,
        trigger_message_id=user_message.id,
        trace_id=trace_id,
    )


CONVERSATION_TITLE_MAX_LEN = 30


def ensure_conversation_title(conversation: Conversation, content: str | None) -> None:
    """首条用户消息自动生成会话标题：仅在标题为空时生效（手动重命名后不覆盖）。

    取消息首行文本、压缩连续空白，超长截断加省略号；内容为空则保持原状。
    """
    if conversation.title:
        return
    text = " ".join((content or "").split())
    if not text:
        return
    conversation.title = text[:CONVERSATION_TITLE_MAX_LEN] + ("…" if len(text) > CONVERSATION_TITLE_MAX_LEN else "")


def _published_agent_version(db: Session, agent_id: UUID | None) -> AgentVersion:
    if agent_id is None:
        raise ApiError("AI_AGENT_VERSION_NOT_PUBLISHED", "agent has no published version", 409)
    agent = db.get(Agent, agent_id)
    if agent is None or agent.current_published_version_id is None:
        raise ApiError("AI_AGENT_VERSION_NOT_PUBLISHED", "agent has no published version", 409)
    agent_version = db.get(AgentVersion, agent.current_published_version_id)
    if agent_version is None:
        raise ApiError("AI_AGENT_VERSION_NOT_PUBLISHED", "agent has no published version", 409)
    return agent_version


async def handle_chat(db: Session, *, ctx: SecurityContext, agent_id: UUID | None, conversation_id: UUID | None, content: str, content_parts: list[dict] | None, idempotency_key: str | None, trace_id: str | None, model_gateway: ModelGateway | None = None, skill_gateway: SkillGateway | None = None, short_memory: ShortTermMemoryProvider | None = None) -> ChatResult:
    tenant_id = ctx.tenant_id
    user_id = ctx.user_id
    trace_id = trace_id or ctx.trace_id
    if idempotency_key:
        existing = db.scalar(select(Task).where(Task.tenant_id == tenant_id, Task.owner_user_id == user_id, Task.idempotency_key == idempotency_key))
        if existing is not None:
            message = db.get(Message, existing.trigger_message_id)
            if message is not None:
                return ChatResult(user_message=message, assistant_message=None, task_id=existing.id, mocked=False)
    if conversation_id is None:
        conversation = _create_conversation(db, tenant_id, user_id, agent_id)
    else:
        conversation = db.scalar(select(Conversation).where(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id, Conversation.owner_user_id == user_id, Conversation.status == "ACTIVE"))
        if conversation is None:
            raise ApiError("AI_RESOURCE_NOT_FOUND", "conversation not found", 404)
        if agent_id is not None and conversation.agent_id != agent_id:
            raise ApiError("AI_CONVERSATION_AGENT_CONFLICT", "conversation agent cannot be changed", 409)
    user_message = Message(tenant_id=tenant_id, conversation_id=conversation.id, sender_type="USER", content=content, content_parts=content_parts, status="PROCESSING")
    db.add(user_message)
    db.flush()
    ensure_conversation_title(conversation, content)
    attachment_ids = _attachment_ids(content_parts)
    if attachment_ids:
        _attach_files(db, user_message, conversation, attachment_ids)
    conversation.last_message_at = datetime.now(timezone.utc)
    if not idempotency_key:
        raise ApiError("AI_PARAM_INVALID", "Idempotency-Key is required", 400)
    if model_gateway is not None:
        agent_version = None
        if conversation.agent_id is not None:
            agent = db.get(Agent, conversation.agent_id)
            if agent is not None and agent.current_published_version_id is not None:
                agent_version = db.get(AgentVersion, agent.current_published_version_id)
        mappings, tools = build_tool_mappings(db, agent_version, tenant_id)
        if mappings and skill_gateway is not None and agent_version is not None:
            # 工具型会话：只创建任务并入队（Outbox TASK_READY），执行由 worker 完成；
            # POST /chat 立即返回 task_id，前端订阅任务事件流获得实时执行过程。
            chat_task = create_chat_task(db, ctx=ctx, conversation=conversation, user_message=user_message, idempotency_key=idempotency_key, trace_id=trace_id)
            return ChatResult(user_message=user_message, assistant_message=None, task_id=chat_task.id, mocked=False)
        if agent_version is not None:
            # 无工具智能体：同步直答（不建任务），回复随响应返回
            system_prompt = build_agent_system_prompt(db, agent_version)
            settings = get_settings()
            # 调用参数：版本快照键优先（快照语义），未设置回退模型档案 max_output_tokens，再退全局兜底
            temperature, max_tokens = snapshot_model_params(agent_version.config_snapshot, default_temperature=settings.model_temperature, default_max_tokens=settings.model_max_tokens)
            model_name = getattr(model_gateway, "default_model", "mock")
            max_tokens = effective_max_tokens(db, tenant_id=tenant_id, model_identifier=model_name, snapshot_max_tokens=(agent_version.config_snapshot or {}).get("max_tokens"), default_max_tokens=settings.model_max_tokens)
            request = ModelRequest(
                trace_id=trace_id or str(user_message.id), conversation_id=conversation.id,
                model=model_name,
                temperature=temperature, max_tokens=max_tokens,
                messages=build_context(db, conversation, system_prompt=system_prompt, attachment_ids=attachment_ids),
                tools=[], timeout_seconds=settings.model_timeout_seconds,
            )
            try:
                response = await model_gateway.generate(request)
            except ModelGatewayError as exc:
                user_message.status = "FAILED"
                raise ApiError(exc.code, exc.message, 502) from exc
            reply = response.content or ""
            assistant = Message(tenant_id=tenant_id, conversation_id=conversation.id, sender_type="ASSISTANT", content=reply, content_parts=assistant_reasoning_parts(response.reasoning_content), status="COMPLETED")
            db.add(assistant)
            db.flush()
            user_message.status = "COMPLETED"
            await _maybe_summarize(db, conversation=conversation, user_id=user_id, trace_id=trace_id, model_gateway=model_gateway, user_message_id=str(user_message.id), short_memory=short_memory)
            return ChatResult(user_message=user_message, assistant_message=assistant, mocked=False, response_content=reply)
    task = create_task(db, ctx=ctx, agent_id=conversation.agent_id, agent_version_id=_version_id(db, conversation.agent_id), idempotency_key=idempotency_key, request_summary=content, conversation_id=conversation.id, trigger_message_id=user_message.id, trace_id=trace_id)
    user_message.status = "COMPLETED"
    return ChatResult(user_message=user_message, assistant_message=None, task_id=task.id, mocked=False)


async def _maybe_summarize(db: Session, *, conversation: Conversation, user_id: UUID, trace_id: str | None, model_gateway: ModelGateway, user_message_id: str, short_memory: ShortTermMemoryProvider | None = None) -> None:
    """会话记忆维护：失败不阻断回复（下一条消息达到阈值时会自动重试）。"""
    if short_memory is None:
        logger.warning("memory.short provider is not injected; skip chat summarization for conversation %s", conversation.id)
        return
    settings = get_settings()
    message_count = db.scalar(select(Message.id).where(Message.conversation_id == conversation.id).order_by(Message.created_at.desc()).offset(settings.memory_short_summary_message_threshold - 1).limit(1))
    if message_count is not None:
        try:
            await short_memory.summarize(db, conversation=conversation, user_id=user_id, trace_id=trace_id or user_message_id, model_gateway=model_gateway)
            # provider 不再自行提交，由调用方收口（P2-18）
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("summarize failed for conversation %s: %s", conversation.id, exc)


def _create_conversation(db: Session, tenant_id: UUID, user_id: UUID, agent_id: UUID | None) -> Conversation:
    query = select(Agent).where(Agent.tenant_id == tenant_id, Agent.status == "ACTIVE", Agent.current_published_version_id.is_not(None), (Agent.owner_user_id == user_id) | (Agent.source_type == "OFFICIAL") | admin_owned_clause(Agent.owner_user_id, tenant_id))
    query = query.where(Agent.id == agent_id) if agent_id else query.where(Agent.is_default_parent.is_(True))
    agent = db.scalar(query)
    if agent is None:
        raise ApiError("AI_AGENT_NOT_FOUND", "agent not found", 404)
    conversation = Conversation(tenant_id=tenant_id, owner_user_id=user_id, agent_id=agent.id, status="ACTIVE")
    db.add(conversation)
    db.flush()
    return conversation


def _version_id(db: Session, agent_id: UUID) -> UUID:
    agent = db.get(Agent, agent_id)
    if agent is None or agent.current_published_version_id is None:
        raise ApiError("AI_AGENT_VERSION_NOT_PUBLISHED", "agent has no published version", 409)
    return agent.current_published_version_id


def _attachment_ids(content_parts: list[dict] | None) -> list[UUID]:
    if not content_parts:
        return []
    values = []
    for part in content_parts:
        value = part.get("file_id") if isinstance(part, dict) else None
        if value:
            try:
                values.append(UUID(str(value)))
            except ValueError:
                raise ApiError("AI_PARAM_INVALID", "attachment file_id is invalid", 400)
    return list(dict.fromkeys(values))


def _attach_files(db: Session, message: Message, conversation: Conversation, file_ids: list[UUID]) -> None:
    from app.models import File

    allowed = db.scalars(select(File).where(File.id.in_(file_ids), File.tenant_id == conversation.tenant_id, File.owner_user_id == conversation.owner_user_id, File.conversation_id == conversation.id, File.status != "DELETED")).all()
    if len(allowed) != len(file_ids):
        raise ApiError("AI_FORBIDDEN", "attachment is not accessible in this conversation", 403)
    db.execute(message_file.insert(), [{"message_id": message.id, "file_id": file.id, "tenant_id": conversation.tenant_id} for file in allowed])
