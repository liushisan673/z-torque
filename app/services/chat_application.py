from datetime import datetime, timezone
from uuid import UUID
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import ApiError
from app.kernel.model_gateway import ModelGateway, ModelRequest, ModelGatewayError
from app.kernel.skill_gateway import SkillGateway
from app.kernel.task.runtime import TaskRuntime
from app.kernel.task.service import create_task, transition_task
from app.kernel.task.states import TaskStatus
from app.models import Agent, AgentVersion, Conversation, Message, Task, Tool, ToolVersion
from app.models.message import message_file
from app.security.permissions import admin_owned_clause
from app.services.chat_service import ChatResult
from app.services.context_builder import build_context
from app.services.execution_loop import AgentExecutionLoop
from app.services.memory_service import summarize_conversation

logger = logging.getLogger("z_torque.chat")


async def handle_chat(db: Session, *, tenant_id: UUID, user_id: UUID, agent_id: UUID | None, conversation_id: UUID | None, content: str, content_parts: list[dict] | None, idempotency_key: str | None, trace_id: str | None, model_gateway: ModelGateway | None = None, skill_gateway: SkillGateway | None = None) -> ChatResult:
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
        mappings, tools = TaskRuntime._build_tool_mappings(db, agent_version, tenant_id)
        system_prompt = (agent_version.config_snapshot or {}).get("system_prompt") if agent_version is not None else None
        # 注入已绑定 skill 的执行指令，让模型了解多工具协作流程（如「先城市搜索拿 LocationID 再查天气」）
        if mappings:
            from app.models import SkillVersion as _SV
            seen_skill_ids: set[UUID] = set()
            instructions: list[str] = []
            for m in mappings.values():
                if m.skill_version_id in seen_skill_ids:
                    continue
                seen_skill_ids.add(m.skill_version_id)
                sv = db.get(_SV, m.skill_version_id)
                if sv is not None and sv.instructions and sv.instructions.strip():
                    instructions.append(sv.instructions.strip())
            if instructions:
                system_prompt = (system_prompt + "\n\n" if system_prompt else "") + "\n\n".join(instructions)
        request = ModelRequest(
            trace_id=trace_id or str(user_message.id), conversation_id=conversation.id,
            model=getattr(model_gateway, "default_model", "mock"),
            messages=build_context(db, conversation, system_prompt=system_prompt, attachment_ids=attachment_ids),
            tools=tools, timeout_seconds=get_settings().model_timeout_seconds,
        )
        content: str | None = None
        chat_task = None
        try:
            if tools and skill_gateway is not None:
                # 工具型会话：创建 RUNNING 任务，支撑写入类工具"强制人工审批"的暂停/重放机制；
                # 事务在路由层统一提交，任务对 worker 不可见，不存在并发抢占。
                chat_task = create_task(
                    db,
                    tenant_id=tenant_id,
                    owner_user_id=user_id,
                    agent_id=conversation.agent_id,
                    agent_version_id=agent_version.id,
                    idempotency_key=idempotency_key,
                    request_summary=content,
                    conversation_id=conversation.id,
                    trigger_message_id=user_message.id,
                    trace_id=trace_id,
                )
                transition_task(db, chat_task, TaskStatus.QUEUED, "TASK_QUEUED", "chat task queued")
                transition_task(db, chat_task, TaskStatus.RUNNING, "TASK_RUNNING", "chat task running")
                loop = AgentExecutionLoop(model_gateway, skill_gateway)
                result = await loop.run(request=request, mappings=mappings, db=db, tenant_id=tenant_id, user_id=user_id, task_id=chat_task.id)
                if result.pending_confirmation is not None:
                    pending_confirmation = _pending_confirmation_info(db, result.pending_confirmation, task_id=chat_task.id)
                    return ChatResult(user_message=user_message, assistant_message=None, task_id=chat_task.id, mocked=False, pending_confirmation=pending_confirmation)
                content = result.response.content
                chat_task.result = {"content": content}
                transition_task(db, chat_task, TaskStatus.SUCCESS, "TASK_SUCCEEDED", "chat task completed")
            else:
                response = await model_gateway.generate(request)
                if not response.tool_calls:
                    content = response.content
        except ModelGatewayError as exc:
            _fail_chat_task(db, chat_task, exc.code, exc.message)
            raise ApiError(exc.code, exc.message, 502) from exc
        except Exception:
            _fail_chat_task(db, chat_task, "AI_TASK_FAILED", "chat task failed")
            raise
        if content:
            assistant = Message(tenant_id=tenant_id, conversation_id=conversation.id, sender_type="ASSISTANT", content=content, status="COMPLETED")
            user_message.status = "COMPLETED"
            db.add(assistant)
            db.flush()
            settings = get_settings()
            message_count = db.scalar(select(Message.id).where(Message.conversation_id == conversation.id).order_by(Message.created_at.desc()).offset(settings.memory_short_summary_message_threshold - 1).limit(1))
            if message_count is not None:
                # 摘要属后台记忆维护：失败不能阻断本次回复（否则整个事务回滚，已生成的回复会丢失），下一条消息达到阈值时会自动重试
                try:
                    await summarize_conversation(db, conversation=conversation, user_id=user_id, trace_id=trace_id or str(user_message.id), model_gateway=model_gateway)
                except Exception as exc:
                    logger.warning("summarize failed for conversation %s: %s", conversation.id, exc)
            return ChatResult(user_message=user_message, assistant_message=assistant, mocked=False)
    task = create_task(db, tenant_id=tenant_id, owner_user_id=user_id, agent_id=conversation.agent_id, agent_version_id=_version_id(db, conversation.agent_id), idempotency_key=idempotency_key, request_summary=content, conversation_id=conversation.id, trigger_message_id=user_message.id, trace_id=trace_id)
    user_message.status = "COMPLETED"
    return ChatResult(user_message=user_message, assistant_message=None, task_id=task.id, mocked=False)


def _pending_confirmation_info(db: Session, info: dict, *, task_id: UUID) -> dict:
    """补全审批卡片信息：任务 ID + 工具名称（工具名需 ToolVersion → Tool 反查）。"""
    result = {
        "confirmation_id": str(info.get("confirmation_id") or ""),
        "task_id": str(task_id),
        "tool_version_id": info.get("tool_version_id"),
        "arguments": info.get("arguments") or {},
    }
    tool_version_id = info.get("tool_version_id")
    if tool_version_id:
        try:
            version = db.get(ToolVersion, UUID(str(tool_version_id)))
            tool = db.get(Tool, version.tool_id) if version else None
            result["tool_name"] = tool.name if tool else None
        except (TypeError, ValueError):
            pass
    return result


def _fail_chat_task(db: Session, task: Task | None, code: str, message: str) -> None:
    """chat 快路径异常收尾：仅当任务仍在 QUEUED/RUNNING 时置为 FAILED，避免覆盖审批挂起状态。"""
    if task is None or task.status not in (TaskStatus.QUEUED, TaskStatus.RUNNING):
        return
    task.error_code = code
    task.error_message = message
    transition_task(db, task, TaskStatus.FAILED, "TASK_CHAT_FAILED", "chat task failed")


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
