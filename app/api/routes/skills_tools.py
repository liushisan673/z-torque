from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.security.permissions import admin_owned_clause
from app.core.responses import success_response
from app.core.servertime import iso_utc
from app.db.session import get_db
from app.kernel.skill_service import archive_resource, attach_tool, copy_skill, copy_tool, create_skill, create_skill_version, create_tool, create_tool_version, delete_resource, detach_tool, publish_skill_version, publish_tool_version, update_skill, update_skill_version
from app.models import Skill, SkillVersion, SkillVersionTool, Tool, ToolVersion, User
from app.schemas.skill import AttachToolRequest, SkillCreateRequest, SkillUpdateRequest, SkillVersionCreateRequest, SkillVersionUpdateRequest, ToolCreateRequest, ToolVersionCreateRequest

router = APIRouter(prefix="/skills-tools", tags=["skills-tools"])


def out(request: Request, value):
    return success_response(request, value)


@router.post("/tools", summary="创建工具")
async def new_tool(payload: ToolCreateRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = create_tool(db, tenant_id=user.tenant_id, user_id=user.id, **payload.model_dump()); db.commit(); db.refresh(item)
    return out(request, {"id": str(item.id), "name": item.name, "tool_type": item.tool_type})


@router.get("/tools", summary="获取工具列表")
async def list_tools(request: Request, include_archived: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    status_filter = Tool.status.in_(["ACTIVE", "ARCHIVED"]) if include_archived else Tool.status == "ACTIVE"
    items = db.scalars(
        select(Tool)
        .where(Tool.tenant_id == user.tenant_id, status_filter, or_(Tool.owner_user_id == user.id, Tool.official.is_(True), admin_owned_clause(Tool.owner_user_id, user.tenant_id)))
        .order_by(Tool.official.desc(), Tool.created_at.desc())
    ).all()
    result = []
    for x in items:
        versions = db.scalars(select(ToolVersion).where(ToolVersion.tool_id == x.id).order_by(ToolVersion.version_number)).all()
        result.append({
            "id": str(x.id),
            "name": x.name,
            "description": x.description,
            "tool_type": x.tool_type,
            "official": x.official,
            "versions": [{"id": str(v.id), "version_number": v.version_number, "status": v.status, "snapshot_hash": v.snapshot_hash} for v in versions],
        })
    return out(request, result)


@router.get("/tools/{tool_id}", summary="获取工具详情")
async def get_tool(tool_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.scalar(select(Tool).where(Tool.id == tool_id, Tool.tenant_id == user.tenant_id, Tool.status.in_(["ACTIVE", "ARCHIVED"]), or_(Tool.owner_user_id == user.id, Tool.official.is_(True), admin_owned_clause(Tool.owner_user_id, user.tenant_id))))
    if item is None:
        from app.core.errors import ApiError
        raise ApiError("AI_RESOURCE_NOT_FOUND", "resource not found", 404)
    versions = db.scalars(select(ToolVersion).where(ToolVersion.tool_id == item.id).order_by(ToolVersion.version_number)).all()
    return out(request, {"id": str(item.id), "name": item.name, "description": item.description, "tool_type": item.tool_type, "official": item.official, "versions": [{"id": str(v.id), "version_number": v.version_number, "status": v.status, "snapshot_hash": v.snapshot_hash} for v in versions]})


@router.get("/tool-versions/{version_id}", summary="获取工具版本详情")
async def get_tool_version(version_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.scalar(select(ToolVersion).where(ToolVersion.id == version_id, ToolVersion.tenant_id == user.tenant_id))
    if item is None:
        from app.core.errors import ApiError
        raise ApiError("AI_TOOL_VERSION_NOT_FOUND", "tool version not found", 404)
    return out(request, {"id": str(item.id), "tool_id": str(item.tool_id), "version_number": item.version_number, "status": item.status, "config_snapshot": item.config_snapshot, "input_schema": item.input_schema, "output_schema": item.output_schema, "required_permissions": item.required_permissions, "requires_confirmation": item.requires_confirmation, "snapshot_hash": item.snapshot_hash})


@router.post("/tools/{tool_id}/archive", summary="归档工具")
async def archive_tool(tool_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = archive_resource(db, tenant_id=user.tenant_id, user_id=user.id, resource_type="TOOL", resource_id=tool_id); db.commit()
    return out(request, {"id": str(item.id), "status": item.status})


@router.delete("/tools/{tool_id}", summary="删除工具")
async def remove_tool(tool_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    delete_resource(db, tenant_id=user.tenant_id, user_id=user.id, resource_type="TOOL", resource_id=tool_id); db.commit()
    return out(request, {"id": str(tool_id), "deleted": True})


@router.post("/tools/{tool_id}/copy", summary="复制工具")
async def duplicate_tool(tool_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = copy_tool(db, tenant_id=user.tenant_id, user_id=user.id, tool_id=tool_id); db.commit(); db.refresh(item)
    return out(request, {"id": str(item.id), "name": item.name, "status": item.status})


@router.post("/tools/{tool_id}/versions", summary="创建工具版本")
async def new_tool_version(tool_id: UUID, payload: ToolVersionCreateRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = create_tool_version(db, tenant_id=user.tenant_id, user_id=user.id, tool_id=tool_id, **payload.model_dump()); db.commit(); db.refresh(item)
    return out(request, {"id": str(item.id), "version_number": item.version_number, "status": item.status})


@router.post("/tool-versions/{version_id}/publish", summary="发布工具版本")
async def publish_tool(version_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = publish_tool_version(db, tenant_id=user.tenant_id, user_id=user.id, version_id=version_id); db.commit()
    return out(request, {"id": str(item.id), "status": item.status, "snapshot_hash": item.snapshot_hash})


@router.post("/skills", summary="创建技能")
async def new_skill(payload: SkillCreateRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = create_skill(db, tenant_id=user.tenant_id, user_id=user.id, **payload.model_dump()); db.commit(); db.refresh(item)
    return out(request, {"id": str(item.id), "name": item.name, "skill_type": item.skill_type})


@router.get("/skills", summary="获取技能列表")
async def list_skills(request: Request, include_archived: bool = False, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    status_filter = Skill.status.in_(["ACTIVE", "ARCHIVED"]) if include_archived else Skill.status == "ACTIVE"
    items = db.scalars(
        select(Skill)
        .where(Skill.tenant_id == user.tenant_id, status_filter, or_(Skill.owner_user_id == user.id, Skill.official.is_(True), admin_owned_clause(Skill.owner_user_id, user.tenant_id)))
        .order_by(Skill.official.desc(), Skill.created_at.desc())
    ).all()
    result = []
    for x in items:
        latest = db.scalar(select(SkillVersion).where(SkillVersion.skill_id == x.id).order_by(SkillVersion.version_number.desc()))
        result.append({
            "id": str(x.id),
            "name": x.name,
            "description": x.description,
            "skill_type": x.skill_type,
            "official": x.official,
            "source_type": x.source_type,
            "owner_user_id": str(x.owner_user_id) if x.owner_user_id else None,
            "status": x.status,
            "created_at": iso_utc(x.created_at),
            "current_published_version_id": str(x.current_published_version_id) if x.current_published_version_id else None,
            "latest_version_id": str(latest.id) if latest else None,
            "latest_version_number": latest.version_number if latest else None,
            "latest_version_status": latest.status if latest else None,
            "has_script": bool(latest and latest.script_source and latest.script_source.strip()) if latest else False,
            # MD-Skill 展示补充字段（来自最新版本快照，避免列表页二次请求）
            "implementation": latest.config_snapshot.get("implementation") if latest else None,
            "relative_path": latest.config_snapshot.get("relative_path") if latest else None,
            "requires_confirmation": latest.requires_confirmation if latest else None,
        })
    return out(request, result)


@router.get("/skills/{skill_id}", summary="获取技能详情")
async def get_skill(skill_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.scalar(select(Skill).where(Skill.id == skill_id, Skill.tenant_id == user.tenant_id, Skill.status.in_(["ACTIVE", "ARCHIVED"]), or_(Skill.owner_user_id == user.id, Skill.official.is_(True), admin_owned_clause(Skill.owner_user_id, user.tenant_id))))
    if item is None:
        from app.core.errors import ApiError
        raise ApiError("AI_RESOURCE_NOT_FOUND", "resource not found", 404)
    versions = db.scalars(select(SkillVersion).where(SkillVersion.skill_id == item.id).order_by(SkillVersion.version_number)).all()
    return out(request, {"id": str(item.id), "name": item.name, "description": item.description, "skill_type": item.skill_type, "official": item.official, "source_type": item.source_type, "owner_user_id": str(item.owner_user_id) if item.owner_user_id else None, "status": item.status, "created_at": iso_utc(item.created_at), "current_published_version_id": str(item.current_published_version_id) if item.current_published_version_id else None, "versions": [{"id": str(v.id), "version_number": v.version_number, "status": v.status, "snapshot_hash": v.snapshot_hash, "published_at": iso_utc(v.published_at)} for v in versions]})


@router.patch("/skills/{skill_id}", summary="更新技能")
async def patch_skill(skill_id: UUID, payload: SkillUpdateRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    values = payload.model_dump(exclude_unset=True)
    # 显式传 description: null 表示清空描述（服务层把 "" 归一化为 None）
    if "description" in values and values["description"] is None:
        values["description"] = ""
    item = update_skill(db, tenant_id=user.tenant_id, user_id=user.id, skill_id=skill_id, **values); db.commit(); db.refresh(item)
    return out(request, {"id": str(item.id), "name": item.name, "description": item.description, "skill_type": item.skill_type, "status": item.status})


@router.get("/skill-versions/{version_id}", summary="获取技能版本详情")
async def get_skill_version(version_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.scalar(select(SkillVersion).where(SkillVersion.id == version_id, SkillVersion.tenant_id == user.tenant_id))
    if item is None:
        from app.core.errors import ApiError
        raise ApiError("AI_SKILL_VERSION_NOT_FOUND", "skill version not found", 404)
    links = db.scalars(select(SkillVersionTool).where(SkillVersionTool.skill_version_id == item.id)).all()
    cfg = item.config_snapshot or {}
    return out(request, {"id": str(item.id), "skill_id": str(item.skill_id), "version_number": item.version_number, "status": item.status, "instructions": item.instructions, "script_source": item.script_source, "required_permissions": item.required_permissions, "requires_confirmation": item.requires_confirmation, "tool_version_ids": [str(link.tool_version_id) for link in links], "snapshot_hash": item.snapshot_hash, "published_at": iso_utc(item.published_at), "implementation": cfg.get("implementation"), "allow_scripts": cfg.get("allow_scripts"), "input_schema": cfg.get("input_schema"), "output_schema": cfg.get("output_schema"), "script_files": cfg.get("script_files"), "declared_version": cfg.get("declared_version"), "relative_path": cfg.get("relative_path")})


@router.post("/skills/{skill_id}/archive", summary="归档技能")
async def archive_skill(skill_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = archive_resource(db, tenant_id=user.tenant_id, user_id=user.id, resource_type="SKILL", resource_id=skill_id); db.commit()
    return out(request, {"id": str(item.id), "status": item.status})


@router.delete("/skills/{skill_id}", summary="删除技能")
async def remove_skill(skill_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    delete_resource(db, tenant_id=user.tenant_id, user_id=user.id, resource_type="SKILL", resource_id=skill_id); db.commit()
    return out(request, {"id": str(skill_id), "deleted": True})


@router.post("/skills/{skill_id}/copy", summary="复制技能")
async def duplicate_skill(skill_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = copy_skill(db, tenant_id=user.tenant_id, user_id=user.id, skill_id=skill_id); db.commit(); db.refresh(item)
    return out(request, {"id": str(item.id), "name": item.name, "status": item.status})


@router.post("/skills/{skill_id}/versions", summary="创建技能版本")
async def new_skill_version(skill_id: UUID, payload: SkillVersionCreateRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = create_skill_version(db, tenant_id=user.tenant_id, user_id=user.id, skill_id=skill_id, **payload.model_dump()); db.commit(); db.refresh(item)
    return out(request, {"id": str(item.id), "version_number": item.version_number, "status": item.status})


@router.post("/skill-versions/{version_id}/tools", summary="为技能版本绑定工具")
async def link_tool(version_id: UUID, payload: AttachToolRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = attach_tool(db, tenant_id=user.tenant_id, user_id=user.id, skill_version_id=version_id, tool_version_id=payload.tool_version_id); db.commit()
    return out(request, {"id": str(item.id), "tool_version_id": str(item.tool_version_id)})


@router.delete("/skill-versions/{version_id}/tools/{tool_version_id}", summary="为技能版本解绑工具")
async def unlink_tool(version_id: UUID, tool_version_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = detach_tool(db, tenant_id=user.tenant_id, user_id=user.id, skill_version_id=version_id, tool_version_id=tool_version_id); db.commit()
    return out(request, {"id": str(item.id), "tool_version_id": str(item.tool_version_id)})


@router.post("/skill-versions/{version_id}/publish", summary="发布技能版本")
async def publish_skill(version_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = publish_skill_version(db, tenant_id=user.tenant_id, user_id=user.id, version_id=version_id); db.commit()
    return out(request, {"id": str(item.id), "status": item.status, "snapshot_hash": item.snapshot_hash})


@router.patch("/skill-versions/{version_id}", summary="更新技能版本")
async def patch_skill_version(version_id: UUID, payload: SkillVersionUpdateRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = update_skill_version(db, tenant_id=user.tenant_id, user_id=user.id, version_id=version_id, **payload.model_dump(exclude_unset=True)); db.commit(); db.refresh(item)
    return out(request, {"id": str(item.id), "version_number": item.version_number, "status": item.status, "instructions": item.instructions, "required_permissions": item.required_permissions, "requires_confirmation": item.requires_confirmation})


@router.get("/skill-versions/{version_id}/tools", summary="获取技能版本工具列表")
async def list_skill_tools(version_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        select(SkillVersionTool, ToolVersion, Tool)
        .join(ToolVersion, ToolVersion.id == SkillVersionTool.tool_version_id)
        .join(Tool, Tool.id == ToolVersion.tool_id)
        .where(SkillVersionTool.skill_version_id == version_id, SkillVersionTool.tenant_id == user.tenant_id)
        .order_by(ToolVersion.version_number)
    ).all()
    return out(request, [
        {"tool_version_id": str(tv.id), "tool_id": str(tool.id), "tool_name": tool.name, "tool_type": tool.tool_type, "description": tool.description, "version_number": tv.version_number, "status": tv.status}
        for _link, tv, tool in rows
    ])
