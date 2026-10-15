from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.security.permissions import admin_owned_clause
from app.core.errors import ApiError
from app.core.responses import success_response
from app.db.session import get_db
from app.kernel.agent.service import archive_agent, copy_agent, create_agent, delete_agent, update_agent
from app.kernel.agent.skill_service import attach_skill_version, detach_skill_version
from app.kernel.agent.version_service import (
    archive_version,
    create_version,
    publish_version,
    update_draft,
)
from app.models import Agent, AgentVersion, AgentVersionSkill, Skill, SkillVersion, User
from app.schemas.agent import (
    AgentCreate,
    AgentResponse,
    AgentUpdate,
    AgentVersionResponse,
    VersionCreate,
)
from app.schemas.agent_skill import AgentSkillAttachRequest

router = APIRouter(prefix="/agents", tags=["agents"])

def out(request, value):
    return success_response(request, value)

@router.get("")
async def list_agents(request: Request, include_archived: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    status_filter = Agent.status.in_(["ACTIVE", "ARCHIVED"]) if include_archived else Agent.status == "ACTIVE"
    rows = db.scalars(select(Agent).where(Agent.tenant_id == user.tenant_id, (Agent.owner_user_id == user.id) | (Agent.source_type == "OFFICIAL") | admin_owned_clause(Agent.owner_user_id, user.tenant_id), status_filter).order_by(Agent.created_at.desc())).all()
    return out(request, [AgentResponse.model_validate(x).model_dump(mode="json") for x in rows])

@router.post("")
async def create(payload: AgentCreate, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = create_agent(db, tenant_id=user.tenant_id, user_id=user.id, **payload.model_dump()); db.commit(); db.refresh(item)
    return out(request, AgentResponse.model_validate(item).model_dump(mode="json"))

def get_agent(db, user, agent_id):
    item = db.scalar(select(Agent).where(Agent.id == agent_id, Agent.tenant_id == user.tenant_id, (Agent.owner_user_id == user.id) | (Agent.source_type == "OFFICIAL") | admin_owned_clause(Agent.owner_user_id, user.tenant_id)))
    if not item: raise ApiError("AI_AGENT_NOT_FOUND", "agent not found", 404)
    return item

@router.get("/{agent_id}")
async def detail(agent_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return out(request, AgentResponse.model_validate(get_agent(db, user, agent_id)).model_dump(mode="json"))

@router.patch("/{agent_id}")
async def update(agent_id: UUID, payload: AgentUpdate, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    values = payload.model_dump(exclude_unset=True)
    # 显式传 description: null 表示清空描述（服务层把 "" 归一化为 None）
    if "description" in values and values["description"] is None:
        values["description"] = ""
    item = update_agent(db, tenant_id=user.tenant_id, user_id=user.id, agent_id=agent_id, **values); db.commit(); db.refresh(item)
    return out(request, AgentResponse.model_validate(item).model_dump(mode="json"))

@router.post("/{agent_id}/copy")
async def copy(agent_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = copy_agent(db, tenant_id=user.tenant_id, user_id=user.id, agent_id=agent_id); db.commit(); db.refresh(item)
    return out(request, AgentResponse.model_validate(item).model_dump(mode="json"))

@router.post("/{agent_id}/archive")
async def archive(agent_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = archive_agent(db, tenant_id=user.tenant_id, user_id=user.id, agent_id=agent_id); db.commit()
    return out(request, AgentResponse.model_validate(item).model_dump(mode="json"))

@router.delete("/{agent_id}")
async def remove(agent_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    delete_agent(db, tenant_id=user.tenant_id, user_id=user.id, agent_id=agent_id); db.commit()
    return out(request, {"id": str(agent_id), "deleted": True})

@router.get("/{agent_id}/versions")
async def versions(agent_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_agent(db, user, agent_id)
    rows = db.scalars(select(AgentVersion).where(AgentVersion.agent_id == agent_id).order_by(AgentVersion.version_number.desc())).all()
    return out(request, [AgentVersionResponse.model_validate(x).model_dump(mode="json") for x in rows])

@router.post("/{agent_id}/versions")
async def create_ver(agent_id: UUID, payload: VersionCreate, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = create_version(db, tenant_id=user.tenant_id, user_id=user.id, agent_id=agent_id, snapshot=payload.config_snapshot); db.commit(); db.refresh(item)
    return out(request, AgentVersionResponse.model_validate(item).model_dump(mode="json"))

@router.get("/{agent_id}/versions/{version_id}")
async def get_ver(agent_id: UUID, version_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_agent(db, user, agent_id); item = db.scalar(select(AgentVersion).where(AgentVersion.id == version_id, AgentVersion.agent_id == agent_id))
    if not item: raise ApiError("AI_AGENT_VERSION_NOT_FOUND", "version not found", 404)
    return out(request, AgentVersionResponse.model_validate(item).model_dump(mode="json"))

@router.patch("/{agent_id}/versions/{version_id}")
async def edit_ver(agent_id: UUID, version_id: UUID, payload: VersionCreate, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_agent(db, user, agent_id)
    # exclude_unset：config_snapshot / 委派字段均「未提供=不修改」
    values = payload.model_dump(exclude_unset=True)
    snapshot = values.pop("config_snapshot", None)
    item = update_draft(db, tenant_id=user.tenant_id, user_id=user.id, version_id=version_id, snapshot=snapshot, delegation_updates=values)
    db.commit(); db.refresh(item)
    return out(request, AgentVersionResponse.model_validate(item).model_dump(mode="json"))

@router.post("/{agent_id}/versions/{version_id}/publish")
async def publish(agent_id: UUID, version_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = publish_version(db, tenant_id=user.tenant_id, user_id=user.id, version_id=version_id); db.commit(); db.refresh(item)
    return out(request, AgentVersionResponse.model_validate(item).model_dump(mode="json"))

@router.post("/{agent_id}/versions/{version_id}/archive")
async def archive_ver(agent_id: UUID, version_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = archive_version(db, tenant_id=user.tenant_id, user_id=user.id, version_id=version_id); db.commit(); db.refresh(item)
    return out(request, AgentVersionResponse.model_validate(item).model_dump(mode="json"))


@router.post("/{agent_id}/versions/{version_id}/skills")
async def attach_skill(agent_id: UUID, version_id: UUID, payload: AgentSkillAttachRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_agent(db, user, agent_id)
    item = attach_skill_version(db, tenant_id=user.tenant_id, user_id=user.id, agent_version_id=version_id, skill_version_id=payload.skill_version_id)
    db.commit()
    return out(request, {"id": str(item.id), "agent_version_id": str(item.agent_version_id), "skill_version_id": str(item.skill_version_id)})


@router.delete("/{agent_id}/versions/{version_id}/skills/{skill_version_id}")
async def detach_skill(agent_id: UUID, version_id: UUID, skill_version_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_agent(db, user, agent_id)
    item = detach_skill_version(db, tenant_id=user.tenant_id, user_id=user.id, agent_version_id=version_id, skill_version_id=skill_version_id)
    db.commit()
    return out(request, {"id": str(item.id), "skill_version_id": str(item.skill_version_id), "detached": True})


@router.get("/{agent_id}/versions/{version_id}/skills")
async def list_skills(agent_id: UUID, version_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_agent(db, user, agent_id)
    rows = db.execute(
        select(AgentVersionSkill, SkillVersion, Skill)
        .join(SkillVersion, SkillVersion.id == AgentVersionSkill.skill_version_id)
        .join(Skill, Skill.id == SkillVersion.skill_id)
        .where(AgentVersionSkill.agent_version_id == version_id, AgentVersionSkill.tenant_id == user.tenant_id)
        .order_by(SkillVersion.version_number)
    ).all()
    return out(request, [
        {
            "skill_version_id": str(sv.id),
            "skill_id": str(skill.id),
            "skill_name": skill.name,
            "version_number": sv.version_number,
            "status": sv.status,
            "script_source": sv.script_source,
            "instructions": sv.instructions,
            "skill_type": skill.skill_type,
            "official": skill.official,
        }
        for _link, sv, skill in rows
    ])