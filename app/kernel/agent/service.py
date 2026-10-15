from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.models import Agent, AgentVersion, AgentVersionSkill, Conversation, ShortTermMemory
from app.kernel.audit.service import record_audit


def _agent(db: Session, tenant_id: UUID, user_id: UUID, agent_id: UUID) -> Agent:
    from app.security.permissions import admin_owned_clause
    agent = db.scalar(select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id, (Agent.owner_user_id == user_id) | (Agent.source_type == "OFFICIAL") | admin_owned_clause(Agent.owner_user_id, tenant_id)))
    if not agent:
        raise ApiError("AI_AGENT_NOT_FOUND", "agent not found", 404)
    return agent


def create_agent(db: Session, *, tenant_id: UUID, user_id: UUID, name: str, description: str | None = None) -> Agent:
    agent = Agent(tenant_id=tenant_id, owner_user_id=user_id, name=name, description=description, source_type="USER", status="ACTIVE")
    db.add(agent)
    db.flush()
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="AGENT_CREATED", resource_type="AGENT", resource_id=agent.id, summary="agent created")
    return agent


def update_agent(db: Session, *, tenant_id: UUID, user_id: UUID, agent_id: UUID, name: str | None = None, description: str | None = None) -> Agent:
    agent = _agent(db, tenant_id, user_id, agent_id)
    if agent.source_type == "OFFICIAL" or agent.owner_user_id != user_id:
        raise ApiError("AI_FORBIDDEN", "official agent is read-only", 403)
    if name is not None:
        agent.name = name.strip()
    if description is not None:
        agent.description = description.strip() or None
    return agent


def copy_agent(db: Session, *, tenant_id: UUID, user_id: UUID, agent_id: UUID) -> Agent:
    source = _agent(db, tenant_id, user_id, agent_id)
    version = db.scalar(select(AgentVersion).where(AgentVersion.id == source.current_published_version_id)) if source.current_published_version_id else None
    agent = Agent(tenant_id=tenant_id, owner_user_id=user_id, name=f"{source.name} Copy", description=source.description, source_type="USER", source_agent_id=source.id, source_agent_version_id=version.id if version else None, status="ACTIVE")
    db.add(agent)
    db.flush()
    if version:
        from app.kernel.agent.version_service import create_version
        copied = create_version(db, tenant_id=tenant_id, user_id=user_id, agent_id=agent.id, snapshot=dict(version.config_snapshot))
        copied.config_schema_version = version.config_schema_version
        copied.delegation_enabled = version.delegation_enabled
        copied.allowed_child_agent_versions = list(version.allowed_child_agent_versions or [])
        copied.child_necessity_policy = dict(version.child_necessity_policy or {})
        copied.delegation_capability_version = version.delegation_capability_version
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="AGENT_COPIED", resource_type="AGENT", resource_id=agent.id, summary="agent copied", detail={"source_agent_id": str(source.id)})
    return agent


def archive_agent(db: Session, *, tenant_id: UUID, user_id: UUID, agent_id: UUID) -> Agent:
    agent = _agent(db, tenant_id, user_id, agent_id)
    if agent.source_type == "OFFICIAL" or agent.owner_user_id != user_id:
        raise ApiError("AI_FORBIDDEN", "agent cannot be archived", 403)
    if agent.is_default_parent:
        raise ApiError("AI_REQUEST_INVALID", "default parent cannot be archived", 422)
    agent.status = "ARCHIVED"
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="AGENT_ARCHIVED", resource_type="AGENT", resource_id=agent.id, summary="agent archived")
    return agent


def delete_agent(db: Session, *, tenant_id: UUID, user_id: UUID, agent_id: UUID) -> None:
    agent = _agent(db, tenant_id, user_id, agent_id)
    if agent.source_type == "OFFICIAL" or agent.owner_user_id != user_id:
        raise ApiError("AI_FORBIDDEN", "agent cannot be deleted", 403)
    if agent.is_default_parent:
        raise ApiError("AI_REQUEST_INVALID", "default parent cannot be deleted", 422)
    if agent.status != "ARCHIVED":
        raise ApiError("AI_REQUEST_INVALID", "agent must be archived before deletion", 409)
    # 会话引用检查：仍有会话的 agent 不允许删除（Task.agent_id 无外键，历史任务保留）
    conv_count = db.scalar(select(func.count(Conversation.id)).where(Conversation.agent_id == agent.id)) or 0
    if conv_count:
        raise ApiError("AI_AGENT_IN_USE", "agent is referenced by conversations; delete or reassign them first", 409)
    # 级联清理：版本技能绑定 → 短期记忆 → 版本 → 本体（按依赖顺序，避免外键阻塞）
    version_ids = [v for v in db.scalars(select(AgentVersion.id).where(AgentVersion.agent_id == agent.id)).all()]
    if version_ids:
        db.query(AgentVersionSkill).filter(AgentVersionSkill.agent_version_id.in_(version_ids)).delete(synchronize_session=False)
    db.query(ShortTermMemory).filter(ShortTermMemory.agent_id == agent.id).delete(synchronize_session=False)
    db.query(AgentVersion).filter(AgentVersion.agent_id == agent.id).delete(synchronize_session=False)
    # 其他 agent 的复制来源引用置空，避免自引用外键阻塞删除
    db.query(Agent).filter(Agent.source_agent_id == agent.id).update(
        {Agent.source_agent_id: None, Agent.source_agent_version_id: None}, synchronize_session=False
    )
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="AGENT_DELETED", resource_type="AGENT", resource_id=agent.id, summary="agent deleted")
    db.delete(agent)


def set_default_parent(db: Session, *, tenant_id: UUID, user_id: UUID, agent_id: UUID) -> Agent:
    agent = _agent(db, tenant_id, user_id, agent_id)
    if agent.source_type != "OFFICIAL" or agent.owner_user_id is not None:
        raise ApiError("AI_FORBIDDEN", "only official agents can be the default parent", 403)
    if agent.status != "ACTIVE" or agent.current_published_version_id is None:
        raise ApiError("AI_REQUEST_INVALID", "default parent requires an active published agent", 422)
    db.query(Agent).filter(Agent.is_default_parent.is_(True), Agent.id != agent.id).update(
        {Agent.is_default_parent: False}, synchronize_session=False
    )
    agent.is_default_parent = True
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="DEFAULT_PARENT_SET", resource_type="AGENT", resource_id=agent.id, summary="default parent set")
    return agent


def bootstrap_default_parent(
    db: Session,
    *,
    tenant_id: UUID,
    owner_user_id: UUID | None,
    name: str = "Z-Torque Default Parent",
    description: str = "Platform default parent Agent",
    snapshot: dict | None = None,
) -> Agent:
    agent = db.scalar(select(Agent).where(Agent.tenant_id == tenant_id, Agent.is_default_parent.is_(True)))
    if agent is not None:
        return agent
    agent = Agent(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        name=name,
        description=description,
        source_type="OFFICIAL",
        status="ACTIVE",
    )
    db.add(agent)
    db.flush()
    version = AgentVersion(
        tenant_id=tenant_id,
        agent_id=agent.id,
        version_number=1,
        status="PUBLISHED",
        config_schema_version="1",
        config_snapshot=snapshot or {"mode": "mock"},
    )
    db.add(version)
    db.flush()
    version.published_at = datetime.now(timezone.utc)
    from app.kernel.agent.version_service import _snapshot_hash
    version.snapshot_hash = _snapshot_hash(version.config_snapshot)
    agent.current_published_version_id = version.id
    agent.is_default_parent = True
    record_audit(db, tenant_id=tenant_id, actor_user_id=owner_user_id, action="DEFAULT_PARENT_BOOTSTRAPPED", resource_type="AGENT", resource_id=agent.id, summary="default parent bootstrapped")
    return agent