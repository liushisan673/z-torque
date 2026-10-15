from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.kernel.audit.service import record_audit
from app.models import AgentVersion, AgentVersionSkill, SkillVersion


def attach_skill_version(db: Session, *, tenant_id: UUID, user_id: UUID, agent_version_id: UUID, skill_version_id: UUID) -> AgentVersionSkill:
    version = db.get(AgentVersion, agent_version_id)
    skill = db.get(SkillVersion, skill_version_id)
    if not version or version.tenant_id != tenant_id:
        raise ApiError("AI_AGENT_VERSION_NOT_FOUND", "agent version not found", 404)
    if version.status != "DRAFT":
        raise ApiError("AI_REQUEST_INVALID", "published agent version is immutable", 422)
    if not skill or skill.tenant_id != tenant_id or skill.status != "PUBLISHED":
        raise ApiError("AI_SKILL_VERSION_NOT_FOUND", "published skill version not found", 404)
    exists = db.scalar(select(AgentVersionSkill).where(AgentVersionSkill.agent_version_id == version.id, AgentVersionSkill.skill_version_id == skill.id))
    if exists:
        raise ApiError("AI_REQUEST_INVALID", "skill version already attached", 422)
    link = AgentVersionSkill(tenant_id=tenant_id, agent_version_id=version.id, skill_version_id=skill.id)
    db.add(link)
    db.flush()
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="AGENT_SKILL_ATTACHED", resource_type="AGENT_VERSION", resource_id=version.id, summary="skill version attached to agent version", detail={"skill_version_id": str(skill.id)})
    return link


def detach_skill_version(db: Session, *, tenant_id: UUID, user_id: UUID, agent_version_id: UUID, skill_version_id: UUID) -> AgentVersionSkill:
    version = db.get(AgentVersion, agent_version_id)
    if not version or version.tenant_id != tenant_id:
        raise ApiError("AI_AGENT_VERSION_NOT_FOUND", "agent version not found", 404)
    if version.status != "DRAFT":
        raise ApiError("AI_REQUEST_INVALID", "published agent version is immutable", 422)
    link = db.scalar(select(AgentVersionSkill).where(AgentVersionSkill.agent_version_id == version.id, AgentVersionSkill.skill_version_id == skill_version_id))
    if not link:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "skill link not found", 404)
    db.delete(link)
    db.flush()
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="AGENT_SKILL_DETACHED", resource_type="AGENT_VERSION", resource_id=version.id, summary="skill version detached from agent version", detail={"skill_version_id": str(skill_version_id)})
    return link
