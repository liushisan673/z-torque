from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.models import Agent, Conversation
from app.security.permissions import admin_owned_clause


def resolve_conversation_agent(db: Session, *, tenant_id: UUID, user_id: UUID, agent_id: UUID | None) -> Agent:
    query = select(Agent).where(
        Agent.tenant_id == tenant_id,
        Agent.status == "ACTIVE",
        Agent.current_published_version_id.is_not(None),
        (Agent.owner_user_id == user_id) | (Agent.source_type == "OFFICIAL") | admin_owned_clause(Agent.owner_user_id, tenant_id),
    )
    query = query.where(Agent.id == agent_id) if agent_id else query.where(Agent.is_default_parent.is_(True))
    agent = db.scalar(query)
    if agent is None:
        raise ApiError("AI_AGENT_NOT_FOUND", "agent not found", 404)
    return agent


def get_owned_conversation(db: Session, *, tenant_id: UUID, user_id: UUID, conversation_id: UUID) -> Conversation:
    conversation = db.scalar(select(Conversation).where(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id, Conversation.owner_user_id == user_id))
    if conversation is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "conversation not found", 404)
    return conversation


def archive_conversation(db: Session, *, tenant_id: UUID, user_id: UUID, conversation_id: UUID) -> Conversation:
    conversation = get_owned_conversation(db, tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id)
    if conversation.status == "ARCHIVED":
        return conversation
    conversation.status = "ARCHIVED"
    conversation.archived_at = datetime.now(timezone.utc)
    return conversation
