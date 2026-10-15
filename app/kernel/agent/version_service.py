from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.models import Agent, AgentVersion, AgentVersionSkill, SkillVersion
from app.kernel.audit.service import record_audit

VERSION_NOT_FOUND = "version not found"


def _snapshot_hash(snapshot: dict) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _owned_agent(db: Session, tenant_id: UUID, user_id: UUID, agent_id: UUID) -> Agent:
    agent = db.scalar(select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id, Agent.owner_user_id == user_id, Agent.source_type == "USER", Agent.status == "ACTIVE"))
    if not agent:
        raise ApiError("AI_AGENT_NOT_FOUND", "agent not found", 404)
    return agent


def create_version(db: Session, *, tenant_id: UUID, user_id: UUID, agent_id: UUID, snapshot: dict) -> AgentVersion:
    _owned_agent(db, tenant_id, user_id, agent_id)
    number = (db.scalar(select(func.max(AgentVersion.version_number)).where(AgentVersion.agent_id == agent_id)) or 0) + 1
    version = AgentVersion(tenant_id=tenant_id, agent_id=agent_id, version_number=number, status="DRAFT", config_schema_version="1", config_snapshot=snapshot)
    db.add(version)
    db.flush()
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="AGENT_VERSION_CREATED", resource_type="AGENT_VERSION", resource_id=version.id, summary="agent version draft created")
    return version


def update_draft(
    db: Session,
    *,
    tenant_id: UUID,
    user_id: UUID,
    version_id: UUID,
    snapshot: dict | None,
    delegation_updates: dict | None = None,
) -> AgentVersion:
    version = db.get(AgentVersion, version_id)
    if not version or version.tenant_id != tenant_id:
        raise ApiError("AI_AGENT_VERSION_NOT_FOUND", VERSION_NOT_FOUND, 404)
    _owned_agent(db, tenant_id, user_id, version.agent_id)
    if version.status != "DRAFT":
        raise ApiError("AI_REQUEST_INVALID", "published version is immutable", 422)
    if snapshot is not None:
        version.config_snapshot = snapshot
    # 委派配置（仅草稿可改）：键存在才更新，显式 null 表示清空
    if delegation_updates:
        if delegation_updates.get("delegation_enabled") is not None:
            version.delegation_enabled = bool(delegation_updates["delegation_enabled"])
        if "allowed_child_agent_versions" in delegation_updates:
            version.allowed_child_agent_versions = delegation_updates["allowed_child_agent_versions"] or []
        if "child_necessity_policy" in delegation_updates:
            version.child_necessity_policy = delegation_updates["child_necessity_policy"]
    return version


def publish_version(db: Session, *, tenant_id: UUID, user_id: UUID, version_id: UUID) -> AgentVersion:
    version = db.get(AgentVersion, version_id)
    if not version or version.tenant_id != tenant_id:
        raise ApiError("AI_AGENT_VERSION_NOT_FOUND", VERSION_NOT_FOUND, 404)
    agent = _owned_agent(db, tenant_id, user_id, version.agent_id)
    if version.status != "DRAFT":
        raise ApiError("AI_REQUEST_INVALID", "version is not a draft", 422)
    if not version.config_snapshot:
        raise ApiError("AI_REQUEST_INVALID", "version configuration is empty", 422)
    for link in db.query(AgentVersionSkill).filter(AgentVersionSkill.agent_version_id == version.id).all():
        skill = db.get(SkillVersion, link.skill_version_id)
        if not skill or skill.tenant_id != tenant_id or skill.status != "PUBLISHED":
            raise ApiError("AI_REQUEST_INVALID", "agent version references an unpublished skill version", 422)
    # 委派白名单校验：引用必须是同租户、已发布版本的 AgentVersion
    if version.delegation_enabled:
        for cid in version.allowed_child_agent_versions or []:
            try:
                child = db.get(AgentVersion, UUID(str(cid)))
            except (ValueError, TypeError):
                raise ApiError("AI_REQUEST_INVALID", "invalid child agent version reference", 422)
            if child is None or child.tenant_id != tenant_id or child.status != "PUBLISHED":
                raise ApiError("AI_REQUEST_INVALID", "delegation references an unpublished child agent version", 422)
    version.status = "PUBLISHED"
    version.snapshot_hash = _snapshot_hash(version.config_snapshot)
    version.published_at = datetime.now(timezone.utc)
    agent.current_published_version_id = version.id
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="AGENT_VERSION_PUBLISHED", resource_type="AGENT_VERSION", resource_id=version.id, summary="agent version published")
    return version


def archive_version(db: Session, *, tenant_id: UUID, user_id: UUID, version_id: UUID) -> AgentVersion:
    version = db.get(AgentVersion, version_id)
    if not version or version.tenant_id != tenant_id:
        raise ApiError("AI_AGENT_VERSION_NOT_FOUND", VERSION_NOT_FOUND, 404)
    agent = _owned_agent(db, tenant_id, user_id, version.agent_id)
    if version.id == agent.current_published_version_id:
        raise ApiError("AI_REQUEST_INVALID", "current version cannot be archived", 422)
    version.status = "ARCHIVED"
    return version