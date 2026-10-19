from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response
from app.core.servertime import iso_utc
from app.db.session import get_db
from app.models import Agent, Conversation, Message, ShortTermMemory, User
from app.schemas.conversation import ConversationCreate, ConversationResponse, ConversationUpdate
from app.schemas.message import MessageResponse
from app.security.permissions import admin_owned_clause
from app.services.memory_service import should_summarize_for_idle, summarize_conversation

router = APIRouter(prefix="/conversations", tags=["conversations"])

def conv(db, user, ident):
    value = db.scalar(select(Conversation).where(Conversation.id == ident, Conversation.tenant_id == user.tenant_id, Conversation.owner_user_id == user.id))
    if not value: raise ApiError("AI_RESOURCE_NOT_FOUND", "conversation not found", 404)
    return value

@router.post("", summary="创建会话")
async def create(payload: ConversationCreate, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = select(Agent).where(
        Agent.tenant_id == user.tenant_id,
        Agent.status == "ACTIVE",
        Agent.current_published_version_id.is_not(None),
        (Agent.owner_user_id == user.id) | (Agent.source_type == "OFFICIAL") | admin_owned_clause(Agent.owner_user_id, user.tenant_id),
    )
    if payload.agent_id is None:
        query = query.where(Agent.is_default_parent.is_(True))
    else:
        query = query.where(Agent.id == payload.agent_id)
    agent = db.scalar(query)
    if not agent: raise ApiError("AI_AGENT_NOT_FOUND", "agent not found", 404)
    item = Conversation(tenant_id=user.tenant_id, owner_user_id=user.id, agent_id=agent.id, title=payload.title, status="ACTIVE")
    db.add(item); db.commit(); db.refresh(item)
    return success_response(request, ConversationResponse.model_validate(item).model_dump(mode="json"))

@router.get("", summary="获取会话列表")
async def list_all(
    request: Request,
    include_archived: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # 默认隐藏已归档会话（归档语义明确）；前端「显示已归档」开关显式传入 include_archived=true
    query = select(Conversation).where(Conversation.tenant_id == user.tenant_id, Conversation.owner_user_id == user.id)
    if not include_archived:
        query = query.where(Conversation.status != "ARCHIVED")
    rows = db.scalars(query.order_by(Conversation.created_at.desc())).all()
    return success_response(request, [ConversationResponse.model_validate(x).model_dump(mode="json") for x in rows])

@router.get("/{conversation_id}", summary="获取会话详情")
async def detail(conversation_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return success_response(request, ConversationResponse.model_validate(conv(db, user, conversation_id)).model_dump(mode="json"))

@router.patch("/{conversation_id}", summary="重命名会话")
async def update(conversation_id: UUID, payload: ConversationUpdate, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = conv(db, user, conversation_id); item.title = payload.title; db.commit(); db.refresh(item)
    return success_response(request, ConversationResponse.model_validate(item).model_dump(mode="json"))

@router.post("/{conversation_id}/archive", summary="归档会话")
async def archive(conversation_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = conv(db, user, conversation_id); item.status = "ARCHIVED"; item.archived_at = datetime.now(timezone.utc); db.commit()
    return success_response(request, ConversationResponse.model_validate(item).model_dump(mode="json"))

@router.post("/{conversation_id}/restore", summary="恢复会话")
async def restore(conversation_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # 归档可恢复：与智能体/技能不同，会话是用户的"工作现场"，误归档必须能找回
    item = conv(db, user, conversation_id); item.status = "ACTIVE"; item.archived_at = None; db.commit()
    return success_response(request, ConversationResponse.model_validate(item).model_dump(mode="json"))

@router.get("/{conversation_id}/messages", summary="获取会话消息列表")
async def messages(conversation_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    conv(db, user, conversation_id)
    rows = db.scalars(select(Message).where(Message.tenant_id == user.tenant_id, Message.conversation_id == conversation_id).order_by(Message.created_at, Message.id)).all()
    return success_response(request, [MessageResponse.model_validate(x).model_dump(mode="json") for x in rows])


@router.get("/{conversation_id}/memory", summary="获取会话记忆")
async def memory(conversation_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    conversation = conv(db, user, conversation_id)
    if should_summarize_for_idle(conversation) and getattr(request.app.state, "model_gateway", None) is not None:
        await summarize_conversation(db, conversation=conversation, user_id=user.id, trace_id=request.state.trace_id, model_gateway=request.app.state.model_gateway)
    item = db.scalar(select(ShortTermMemory).where(ShortTermMemory.tenant_id == user.tenant_id, ShortTermMemory.user_id == user.id, ShortTermMemory.agent_id == conversation.agent_id, ShortTermMemory.conversation_id == conversation.id, ShortTermMemory.status == "ACTIVE").order_by(ShortTermMemory.updated_at.desc()))
    return success_response(request, {"summary": item.summary, "source_start": item.source_start, "source_end": item.source_end, "updated_at": iso_utc(item.updated_at)} if item else None)


@router.post("/{conversation_id}/memory/summarize", summary="生成会话记忆摘要")
async def summarize(conversation_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    conversation = conv(db, user, conversation_id)
    gateway = getattr(request.app.state, "model_gateway", None)
    if gateway is None:
        raise ApiError("AI_MODEL_NOT_AVAILABLE", "model gateway is unavailable", 503)
    item = await summarize_conversation(db, conversation=conversation, user_id=user.id, trace_id=request.state.trace_id, model_gateway=gateway)
    return success_response(request, {"summary": item.summary, "source_start": item.source_start, "source_end": item.source_end, "updated_at": iso_utc(item.updated_at)})