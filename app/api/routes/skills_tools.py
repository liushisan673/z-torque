from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.security.permissions import admin_owned_clause
from app.core.limits import MAX_SKILL_BOUND_TOOLS
from app.core.responses import success_response
from app.core.servertime import iso_utc
from app.db.session import get_db
from app.kernel.skill_service import archive_resource, attach_tool, copy_skill, copy_tool, create_skill, create_skill_version, create_tool, create_tool_version, delete_resource, detach_tool, publish_skill_version, publish_tool_version, reorder_tool_bindings, update_skill, update_skill_version, update_tool_binding, update_tool_metadata, upgrade_tool_binding
from app.models import Connector, ConnectorTool, Skill, SkillVersion, SkillVersionTool, Tool, ToolVersion, User
from app.schemas.skill import AttachToolRequest, SkillCreateRequest, SkillUpdateRequest, SkillVersionCreateRequest, SkillVersionUpdateRequest, ToolBindingOrderRequest, ToolBindingUpdateRequest, ToolBindingUpgradeRequest, ToolCreateRequest, ToolPatchRequest, ToolVersionCreateRequest

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
            "tags": list(x.tags or []),
            "is_business_tool": bool(x.is_business_tool),
            "versions": [{"id": str(v.id), "version_number": v.version_number, "status": v.status, "snapshot_hash": v.snapshot_hash} for v in versions],
        })
    return out(request, result)


def _param_summary(schema: dict) -> str:
    """从 input_schema 派生简短参数摘要，不落库避免漂移。"""
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return ""
    return ", ".join(str(name) for name in list(props.keys())[:8])[:160]


def _schema_params(schema: dict, limit: int = 12) -> list[dict]:
    """从 input_schema 派生结构化参数清单（必填在前）：绑定选择器/封装弹窗统一渲染。"""
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = {str(r) for r in (schema.get("required") or [])} if isinstance(schema.get("required"), list) else set()
    items = []
    for name, prop in props.items():
        prop = prop if isinstance(prop, dict) else {}
        items.append({
            "name": str(name),
            "type": str(prop.get("type") or "string"),
            "description": prop.get("description"),
            "required": str(name) in required,
        })
    items.sort(key=lambda item: not item["required"])
    return items[:limit]


def _tool_search_text(tool: Tool, versions: list[ToolVersion]) -> str:
    parts = [tool.name or "", tool.description or "", " ".join(tool.tags or [])]
    for version in versions:
        cfg = version.config_snapshot or {}
        parts.append(str(cfg.get("url") or ""))
        openapi = cfg.get("openapi") if isinstance(cfg.get("openapi"), dict) else {}
        parts.append(str(openapi.get("path") or ""))
        parts.append(str(cfg.get("external_name") or ""))
    return " ".join(parts).lower()


@router.get("/tools/selectable", summary="获取可选工具（按连接器分组）")
async def selectable_tools(
    request: Request,
    q: str = "",
    connector_id: str = "",
    tool_type: str = "",
    tag: str = "",
    skill_version_id: UUID | None = None,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=1000),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Skill 绑定选择器专用：只返回含已发布版本的 ACTIVE 工具，按连接器分组。

    - connector_id 传 `none` 表示未关联连接器的工具；
    - tag 支持逗号分隔，命中任一标签即匹配；
    - 返回的 total 为过滤后的工具总数，limit 为单 Skill 绑定上限。
    """
    tool_filter = [
        Tool.tenant_id == user.tenant_id,
        Tool.status == "ACTIVE",
        or_(Tool.owner_user_id == user.id, Tool.official.is_(True), admin_owned_clause(Tool.owner_user_id, user.tenant_id)),
    ]
    if tool_type.strip():
        tool_filter.append(Tool.tool_type == tool_type.strip())
    tools = db.scalars(select(Tool).where(*tool_filter).order_by(Tool.created_at)).all()
    tool_ids = [item.id for item in tools]

    # 连接器归属：ConnectorTool 为权威来源（不使用 config_snapshot 字符串推断）
    # 仅统计 ENABLED 连接器：归档/停用连接器下的工具执行时必然失败，不应出现在绑定选择器
    linked_tool_ids: set[UUID] = set()
    connectors_by_tool: dict[UUID, list[tuple[UUID, str]]] = {}
    if tool_ids:
        all_rows = db.execute(
            select(ConnectorTool.tool_id).where(ConnectorTool.tool_id.in_(tool_ids), ConnectorTool.tenant_id == user.tenant_id)
        ).all()
        linked_tool_ids = {row[0] for row in all_rows}
        rows = db.execute(
            select(ConnectorTool.tool_id, Connector.id, Connector.name)
            .join(Connector, Connector.id == ConnectorTool.connector_id)
            .where(
                ConnectorTool.tool_id.in_(tool_ids),
                ConnectorTool.tenant_id == user.tenant_id,
                Connector.tenant_id == user.tenant_id,
                Connector.status == "ENABLED",
            )
        ).all()
        for tool_id, cid, cname in rows:
            connectors_by_tool.setdefault(tool_id, []).append((cid, cname))

    published_by_tool: dict[UUID, list[ToolVersion]] = {}
    if tool_ids:
        versions = db.scalars(
            select(ToolVersion)
            .where(ToolVersion.tool_id.in_(tool_ids), ToolVersion.tenant_id == user.tenant_id, ToolVersion.status == "PUBLISHED")
            .order_by(ToolVersion.version_number)
        ).all()
        for version in versions:
            published_by_tool.setdefault(version.tool_id, []).append(version)

    bound_ids: set[str] = set()
    if skill_version_id is not None:
        bound_ids = {
            str(row.tool_version_id)
            for row in db.scalars(select(SkillVersionTool).where(SkillVersionTool.skill_version_id == skill_version_id)).all()
        }

    keywords = q.strip().lower()
    wanted_tags = [item.strip() for item in tag.split(",") if item.strip()]
    wanted_connector = connector_id.strip()
    entries: list[dict] = []
    for tool in tools:
        published = published_by_tool.get(tool.id) or []
        if not published:
            continue
        links = connectors_by_tool.get(tool.id) or []
        # 关联了连接器但没有任何 ENABLED 连接器：工具不可执行，直接跳过
        if tool.id in linked_tool_ids and not links:
            continue
        if wanted_connector:
            if wanted_connector == "none" and links:
                continue
            if wanted_connector != "none" and not any(str(cid) == wanted_connector for cid, _ in links):
                continue
        if wanted_tags and not (set(wanted_tags) & set(tool.tags or [])):
            continue
        if keywords and keywords not in _tool_search_text(tool, published):
            continue
        default_version = next((v for v in published if v.id == tool.current_published_version_id), published[-1])
        entries.append({
            "tool_id": str(tool.id),
            "name": tool.name,
            "description": tool.description,
            "tool_type": tool.tool_type,
            "tags": list(tool.tags or []),
            "is_business_tool": bool(tool.is_business_tool),
            "requires_confirmation": bool(default_version.requires_confirmation),
            "param_summary": _param_summary(default_version.input_schema or {}),
            "params": _schema_params(default_version.input_schema or {}),
            "default_tool_version_id": str(default_version.id),
            "published_versions": [{"tool_version_id": str(v.id), "version_number": v.version_number} for v in published],
            "bound_to_current_version": str(default_version.id) in bound_ids,
            "connectors": [{"connector_id": str(cid), "connector_name": cname} for cid, cname in links],
        })

    # 有连接器的分组在前，未关联连接器的分组固定在最后
    entries.sort(key=lambda item: (0 if item["connectors"] else 1, item["connectors"][0]["connector_name"] if item["connectors"] else "", item["name"]))
    total = len(entries)
    start = (page - 1) * size
    page_entries = entries[start : start + size]

    groups: dict[str, dict] = {}
    for entry in page_entries:
        if entry["connectors"]:
            key = entry["connectors"][0]["connector_id"]
            group = groups.setdefault(key, {"connector_id": key, "connector_name": entry["connectors"][0]["connector_name"], "tools": []})
        else:
            group = groups.setdefault("none", {"connector_id": None, "connector_name": "未关联连接器", "tools": []})
        group["tools"].append(entry)
    return out(request, {"groups": list(groups.values()), "total": total, "limit": MAX_SKILL_BOUND_TOOLS})


@router.patch("/tools/{tool_id}", summary="更新工具元数据")
async def patch_tool(tool_id: UUID, payload: ToolPatchRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = update_tool_metadata(db, tenant_id=user.tenant_id, user_id=user.id, tool_id=tool_id, **payload.model_dump(exclude_unset=True)); db.commit(); db.refresh(item)
    return out(request, {"id": str(item.id), "name": item.name, "description": item.description, "tags": list(item.tags or []), "is_business_tool": bool(item.is_business_tool)})


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
    return out(request, {"id": str(item.id), "name": item.name, "description": item.description, "skill_type": item.skill_type, "official": item.official, "source_type": item.source_type, "owner_user_id": str(item.owner_user_id) if item.owner_user_id else None, "status": item.status, "created_at": iso_utc(item.created_at), "current_published_version_id": str(item.current_published_version_id) if item.current_published_version_id else None, "versions": [{"id": str(v.id), "version_number": v.version_number, "status": v.status, "snapshot_hash": v.snapshot_hash, "published_at": iso_utc(v.published_at), "requires_confirmation": bool(v.requires_confirmation)} for v in versions]})


@router.patch("/skills/{skill_id}", summary="更新技能")
async def patch_skill(skill_id: UUID, payload: SkillUpdateRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    values = payload.model_dump(exclude_unset=True)
    # 显式传 description: null 表示清空描述（服务层把 "" 归一化为 None）
    if "description" in values and values["description"] is None:
        values["description"] = ""
    item = update_skill(db, tenant_id=user.tenant_id, user_id=user.id, skill_id=skill_id, **values); db.commit(); db.refresh(item)
    return out(request, {"id": str(item.id), "name": item.name, "description": item.description, "skill_type": item.skill_type, "status": item.status})


@router.get("/skill-versions/selectable", summary="获取可选技能版本（按技能分组）")
async def selectable_skill_versions(
    request: Request,
    q: str = "",
    source: str = "all",
    skill_type: str = "",
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Agent 绑定技能版本专用：一次返回分组结果，替代逐条 Skill 详情请求（N+1）。"""
    filters = [
        Skill.tenant_id == user.tenant_id,
        Skill.status == "ACTIVE",
        or_(Skill.owner_user_id == user.id, Skill.official.is_(True), admin_owned_clause(Skill.owner_user_id, user.tenant_id)),
    ]
    if source == "mine":
        filters.append(Skill.owner_user_id == user.id)
    elif source == "official":
        filters.append(Skill.official.is_(True))
    if skill_type.strip():
        filters.append(Skill.skill_type == skill_type.strip())
    keywords = q.strip().lower()
    skills = db.scalars(select(Skill).where(*filters).order_by(Skill.official.desc(), Skill.created_at.desc())).all()
    if keywords:
        skills = [item for item in skills if keywords in f"{item.name} {item.description or ''}".lower()]
    total = len(skills)
    start = (page - 1) * size
    page_skills = skills[start : start + size]
    skill_ids = [item.id for item in page_skills]
    versions_by_skill: dict[UUID, list[SkillVersion]] = {}
    tool_counts: dict[UUID, int] = {}
    if skill_ids:
        versions = db.scalars(
            select(SkillVersion)
            .where(SkillVersion.skill_id.in_(skill_ids), SkillVersion.tenant_id == user.tenant_id, SkillVersion.status == "PUBLISHED")
            .order_by(SkillVersion.version_number.desc())
        ).all()
        for version in versions:
            versions_by_skill.setdefault(version.skill_id, []).append(version)
        if versions:
            rows = db.execute(
                select(SkillVersionTool.skill_version_id, func.count(SkillVersionTool.id))
                .where(SkillVersionTool.skill_version_id.in_([v.id for v in versions]))
                .group_by(SkillVersionTool.skill_version_id)
            ).all()
            tool_counts = {row[0]: row[1] for row in rows}
    groups = []
    for skill in page_skills:
        versions = versions_by_skill.get(skill.id) or []
        if not versions:
            continue
        groups.append({
            "skill_id": str(skill.id),
            "skill_name": skill.name,
            "description": skill.description,
            "skill_type": skill.skill_type,
            "official": skill.official,
            "source_type": skill.source_type,
            "versions": [
                {
                    "skill_version_id": str(version.id),
                    "version_number": version.version_number,
                    "published_at": iso_utc(version.published_at),
                    "tool_count": tool_counts.get(version.id, 0),
                    "mode": (version.config_snapshot or {}).get("implementation") or ("script" if version.script_source else "instruction"),
                    "requires_confirmation": version.requires_confirmation,
                    "orchestration_hint": version.orchestration_hint,
                }
                for version in versions
            ],
        })
    return out(request, {"groups": groups, "total": total})


@router.get("/skill-versions/{version_id}", summary="获取技能版本详情")
async def get_skill_version(version_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.scalar(select(SkillVersion).where(SkillVersion.id == version_id, SkillVersion.tenant_id == user.tenant_id))
    if item is None:
        from app.core.errors import ApiError
        raise ApiError("AI_SKILL_VERSION_NOT_FOUND", "skill version not found", 404)
    links = db.scalars(
        select(SkillVersionTool)
        .where(SkillVersionTool.skill_version_id == item.id)
        .order_by(SkillVersionTool.sort_order, SkillVersionTool.created_at)
    ).all()
    cfg = item.config_snapshot or {}
    return out(request, {
        "id": str(item.id),
        "skill_id": str(item.skill_id),
        "version_number": item.version_number,
        "status": item.status,
        "instructions": item.instructions,
        "orchestration_hint": item.orchestration_hint,
        "script_source": item.script_source,
        "required_permissions": item.required_permissions,
        "requires_confirmation": item.requires_confirmation,
        "tool_version_ids": [str(link.tool_version_id) for link in links],
        "tool_bindings": [
            {"tool_version_id": str(link.tool_version_id), "sort_order": link.sort_order, "role": link.role, "note": link.note}
            for link in links
        ],
        "snapshot_hash": item.snapshot_hash,
        "published_at": iso_utc(item.published_at),
        "implementation": cfg.get("implementation"),
        "allow_scripts": cfg.get("allow_scripts"),
        "input_schema": cfg.get("input_schema"),
        "output_schema": cfg.get("output_schema"),
        "script_files": cfg.get("script_files"),
        "declared_version": cfg.get("declared_version"),
        "relative_path": cfg.get("relative_path"),
    })


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
    item = attach_tool(db, tenant_id=user.tenant_id, user_id=user.id, skill_version_id=version_id, tool_version_id=payload.tool_version_id, sort_order=payload.sort_order, role=payload.role, note=payload.note); db.commit()
    return out(request, {"id": str(item.id), "tool_version_id": str(item.tool_version_id), "sort_order": item.sort_order, "role": item.role, "note": item.note})


@router.delete("/skill-versions/{version_id}/tools/{tool_version_id}", summary="为技能版本解绑工具")
async def unlink_tool(version_id: UUID, tool_version_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = detach_tool(db, tenant_id=user.tenant_id, user_id=user.id, skill_version_id=version_id, tool_version_id=tool_version_id); db.commit()
    return out(request, {"id": str(item.id), "tool_version_id": str(item.tool_version_id)})


@router.patch("/skill-versions/{version_id}/tools/{tool_version_id}", summary="更新工具绑定顺序/角色/备注")
async def patch_tool_binding(version_id: UUID, tool_version_id: UUID, payload: ToolBindingUpdateRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = update_tool_binding(db, tenant_id=user.tenant_id, user_id=user.id, skill_version_id=version_id, tool_version_id=tool_version_id, **payload.model_dump(exclude_unset=True)); db.commit()
    return out(request, {"tool_version_id": str(item.tool_version_id), "sort_order": item.sort_order, "role": item.role, "note": item.note})


@router.put("/skill-versions/{version_id}/tools/order", summary="重排工具绑定顺序")
async def reorder_tools(version_id: UUID, payload: ToolBindingOrderRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    items = reorder_tool_bindings(db, tenant_id=user.tenant_id, user_id=user.id, skill_version_id=version_id, tool_version_ids=payload.tool_version_ids); db.commit()
    return out(request, [{"tool_version_id": str(item.tool_version_id), "sort_order": item.sort_order} for item in items])


@router.post("/skill-versions/{version_id}/publish", summary="发布技能版本")
async def publish_skill(version_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = publish_skill_version(db, tenant_id=user.tenant_id, user_id=user.id, version_id=version_id); db.commit()
    return out(request, {"id": str(item.id), "status": item.status, "snapshot_hash": item.snapshot_hash})


@router.patch("/skill-versions/{version_id}", summary="更新技能版本")
async def patch_skill_version(version_id: UUID, payload: SkillVersionUpdateRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = update_skill_version(db, tenant_id=user.tenant_id, user_id=user.id, version_id=version_id, **payload.model_dump(exclude_unset=True)); db.commit(); db.refresh(item)
    return out(request, {"id": str(item.id), "version_number": item.version_number, "status": item.status, "instructions": item.instructions, "required_permissions": item.required_permissions, "requires_confirmation": item.requires_confirmation})


@router.post("/skill-versions/{version_id}/tools/{tool_version_id}/upgrade", summary="升级工具绑定到新发布版本")
async def upgrade_skill_tool(version_id: UUID, tool_version_id: UUID, payload: ToolBindingUpgradeRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = upgrade_tool_binding(db, tenant_id=user.tenant_id, user_id=user.id, skill_version_id=version_id, tool_version_id=tool_version_id, new_tool_version_id=payload.new_tool_version_id); db.commit()
    return out(request, {"tool_version_id": str(item.tool_version_id), "sort_order": item.sort_order, "role": item.role, "note": item.note})


@router.get("/skill-versions/{version_id}/tools", summary="获取技能版本工具列表")
async def list_skill_tools(version_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        select(SkillVersionTool, ToolVersion, Tool)
        .join(ToolVersion, ToolVersion.id == SkillVersionTool.tool_version_id)
        .join(Tool, Tool.id == ToolVersion.tool_id)
        .where(SkillVersionTool.skill_version_id == version_id, SkillVersionTool.tenant_id == user.tenant_id)
        .order_by(SkillVersionTool.sort_order, SkillVersionTool.created_at)
    ).all()
    # 当前发布版本号：用于前端提示"工具有新版本，可升级绑定"
    current_ids = {tool.current_published_version_id for _, _, tool in rows if tool.current_published_version_id}
    current_numbers: dict = {}
    if current_ids:
        for v in db.scalars(select(ToolVersion).where(ToolVersion.id.in_(current_ids))).all():
            current_numbers[v.id] = v.version_number
    return out(request, [
        {"tool_version_id": str(tv.id), "tool_id": str(tool.id), "tool_name": tool.name, "tool_type": tool.tool_type, "description": tool.description, "version_number": tv.version_number, "status": tv.status, "tags": list(tool.tags or []), "is_business_tool": bool(tool.is_business_tool), "sort_order": link.sort_order, "role": link.role, "note": link.note, "current_published_version_id": str(tool.current_published_version_id) if tool.current_published_version_id else None, "current_version_number": current_numbers.get(tool.current_published_version_id) if tool.current_published_version_id else None}
        for link, tv, tool in rows
    ])
