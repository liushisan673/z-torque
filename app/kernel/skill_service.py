from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.limits import MAX_SKILL_BOUND_TOOLS
from app.kernel.agent.version_service import _snapshot_hash
from app.kernel.audit.service import record_audit
from app.models import (
    AgentVersionSkill,
    ConnectorTool,
    Skill,
    SkillExecutionLog,
    SkillVersion,
    SkillVersionTool,
    Tool,
    ToolVersion,
)

MAX_TOOL_TAGS = 20
MAX_TOOL_TAG_LENGTH = 32

_BINDING_ROLES = {"prerequisite", "normal", "followup"}


def normalize_tags(tags: list[str] | None) -> list[str]:
    """标签规范化：去空白、去重、限长限量，保持首次出现顺序。"""
    if not tags:
        return []
    result: list[str] = []
    for raw in tags:
        value = str(raw or "").strip()
        if not value:
            continue
        value = value[:MAX_TOOL_TAG_LENGTH]
        if value not in result:
            result.append(value)
        if len(result) >= MAX_TOOL_TAGS:
            break
    return result


def validate_binding_role(role: str | None) -> str | None:
    if role is None:
        return None
    value = str(role).strip() or None
    if value is not None and value not in _BINDING_ROLES:
        raise ApiError("AI_REQUEST_INVALID", "unsupported tool binding role", 422)
    return value


def _binding_links(db: Session, skill_version_id: UUID) -> list[SkillVersionTool]:
    return db.scalars(
        select(SkillVersionTool)
        .where(SkillVersionTool.skill_version_id == skill_version_id)
        .order_by(SkillVersionTool.sort_order, SkillVersionTool.created_at)
    ).all()


def _owned(db: Session, model, tenant_id: UUID, user_id: UUID, item_id: UUID):
    item = db.scalar(select(model).where(model.id == item_id, model.tenant_id == tenant_id, model.owner_user_id == user_id, model.source_type == "USER", model.status == "ACTIVE"))
    if not item:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "resource not found", 404)
    return item


def _owned_any_status(db: Session, model, tenant_id: UUID, user_id: UUID, item_id: UUID):
    # 归档态资源已被 _owned 的 ACTIVE 过滤排除，删除操作需单独查询；
    # 不限制 source_type（连接器注册的工具 source_type 为 CONNECTOR，同样允许其所有者删除）
    item = db.scalar(select(model).where(model.id == item_id, model.tenant_id == tenant_id, model.owner_user_id == user_id))
    if not item:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "resource not found", 404)
    return item


def create_tool(db: Session, *, tenant_id: UUID, user_id: UUID, name: str, description: str | None, tool_type: str) -> Tool:
    item = Tool(tenant_id=tenant_id, owner_user_id=user_id, name=name, description=description, tool_type=tool_type, source_type="USER", status="ACTIVE", official=False)
    db.add(item); db.flush()
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="TOOL_CREATED", resource_type="TOOL", resource_id=item.id, summary="tool created")
    return item


def update_tool_metadata(db: Session, *, tenant_id: UUID, user_id: UUID, tool_id: UUID, name: str | None = None, description: str | None = None, tags: list[str] | None = None, is_business_tool: bool | None = None) -> Tool:
    """更新 Tool 级可变元数据（标签、业务封装标识、名称/描述）。

    仅所有者可改；连接器注册的工具（source_type=CONNECTOR）同样允许注册者编辑标签。
    """
    item = db.scalar(select(Tool).where(Tool.id == tool_id, Tool.tenant_id == tenant_id, Tool.owner_user_id == user_id))
    if item is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "resource not found", 404)
    if item.official:
        raise ApiError("AI_RESOURCE_FORBIDDEN", "official resources cannot be modified", 403)
    if item.status != "ACTIVE":
        raise ApiError("AI_REQUEST_INVALID", "archived tool cannot be modified", 409)
    changed: list[str] = []
    if name is not None:
        item.name = name.strip()
        changed.append("name")
    if description is not None:
        item.description = description.strip() or None
        changed.append("description")
    if tags is not None:
        item.tags = normalize_tags(tags)
        changed.append("tags")
    if is_business_tool is not None:
        item.is_business_tool = bool(is_business_tool)
        changed.append("is_business_tool")
    if changed:
        record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="TOOL_UPDATED", resource_type="TOOL", resource_id=item.id, summary="tool metadata updated", detail={"fields": changed})
    return item


def create_skill(db: Session, *, tenant_id: UUID, user_id: UUID, name: str, description: str | None, skill_type: str = "STANDARD") -> Skill:
    item = Skill(tenant_id=tenant_id, owner_user_id=user_id, name=name, description=description, skill_type=skill_type, source_type="USER", status="ACTIVE", official=False)
    db.add(item); db.flush()
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="SKILL_CREATED", resource_type="SKILL", resource_id=item.id, summary="skill created")
    return item


def update_skill(db: Session, *, tenant_id: UUID, user_id: UUID, skill_id: UUID, name: str | None = None, description: str | None = None) -> Skill:
    item = _owned(db, Skill, tenant_id, user_id, skill_id)
    changed: list[str] = []
    if name is not None:
        item.name = name.strip()
        changed.append("name")
    if description is not None:
        item.description = description.strip() or None
        changed.append("description")
    if changed:
        record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="SKILL_UPDATED", resource_type="SKILL", resource_id=item.id, summary="skill updated", detail={"fields": changed})
    return item


def copy_tool(db: Session, *, tenant_id: UUID, user_id: UUID, tool_id: UUID) -> Tool:
    source = db.scalar(select(Tool).where(Tool.id == tool_id, Tool.tenant_id == tenant_id, Tool.status == "ACTIVE"))
    if not source:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "resource not found", 404)
    version = db.scalar(select(ToolVersion).where(ToolVersion.id == source.current_published_version_id, ToolVersion.status == "PUBLISHED"))
    item = Tool(tenant_id=tenant_id, owner_user_id=user_id, name=f"{source.name} copy", description=source.description, tool_type=source.tool_type, source_type="USER", status="ACTIVE", official=False)
    db.add(item); db.flush()
    if version:
        copied = ToolVersion(tenant_id=tenant_id, tool_id=item.id, version_number=1, status="DRAFT", config_snapshot=dict(version.config_snapshot), input_schema=dict(version.input_schema), output_schema=dict(version.output_schema) if version.output_schema else None, required_permissions=list(version.required_permissions), requires_confirmation=version.requires_confirmation, timeout_seconds=version.timeout_seconds, max_retries=version.max_retries, retry_backoff_seconds=version.retry_backoff_seconds)
        copied.config_snapshot["source_tool_id"] = str(source.id)
        copied.config_snapshot["source_tool_version_id"] = str(version.id)
        db.add(copied)
    db.flush()
    return item


def copy_skill(db: Session, *, tenant_id: UUID, user_id: UUID, skill_id: UUID) -> Skill:
    source = db.scalar(select(Skill).where(Skill.id == skill_id, Skill.tenant_id == tenant_id, Skill.status == "ACTIVE"))
    if not source:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "resource not found", 404)
    version = db.scalar(select(SkillVersion).where(SkillVersion.id == source.current_published_version_id, SkillVersion.status == "PUBLISHED"))
    item = Skill(tenant_id=tenant_id, owner_user_id=user_id, name=f"{source.name} copy", description=source.description, skill_type=source.skill_type, source_type="USER", status="ACTIVE", official=False)
    db.add(item); db.flush()
    if version:
        copied = SkillVersion(tenant_id=tenant_id, skill_id=item.id, version_number=1, status="DRAFT", config_snapshot={**version.config_snapshot, "source_skill_id": str(source.id), "source_skill_version_id": str(version.id)}, instructions=version.instructions, orchestration_hint=version.orchestration_hint, script_source=version.script_source, required_permissions=list(version.required_permissions), requires_confirmation=version.requires_confirmation)
        db.add(copied); db.flush()
        links = _binding_links(db, version.id)
        for link in links:
            db.add(SkillVersionTool(tenant_id=tenant_id, skill_version_id=copied.id, tool_version_id=link.tool_version_id, sort_order=link.sort_order, role=link.role, note=link.note))
    db.flush()
    return item


def archive_resource(db: Session, *, tenant_id: UUID, user_id: UUID, resource_type: str, resource_id: UUID):
    model = Tool if resource_type == "TOOL" else Skill if resource_type == "SKILL" else None
    if model is None:
        raise ApiError("AI_REQUEST_INVALID", "unsupported resource type", 422)
    # 不限制 source_type：连接器注册的工具（source_type=CONNECTOR）同样允许其所有者归档
    item = _owned_any_status(db, model, tenant_id, user_id, resource_id)
    if item.official:
        raise ApiError("AI_RESOURCE_FORBIDDEN", "official resources cannot be archived", 403)
    if item.status == "ARCHIVED":
        raise ApiError("AI_REQUEST_INVALID", "resource is already archived", 409)
    item.status = "ARCHIVED"
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action=f"{resource_type}_ARCHIVED", resource_type=resource_type, resource_id=item.id, summary="resource archived")
    return item


def delete_resource(db: Session, *, tenant_id: UUID, user_id: UUID, resource_type: str, resource_id: UUID) -> None:
    model = Tool if resource_type == "TOOL" else Skill if resource_type == "SKILL" else None
    if model is None:
        raise ApiError("AI_REQUEST_INVALID", "unsupported resource type", 422)
    item = _owned_any_status(db, model, tenant_id, user_id, resource_id)
    if item.official:
        raise ApiError("AI_RESOURCE_FORBIDDEN", "official resources cannot be deleted", 403)
    if item.status != "ARCHIVED":
        raise ApiError("AI_REQUEST_INVALID", "resource must be archived before deletion", 409)
    if resource_type == "TOOL":
        version_ids = [v for v in db.scalars(select(ToolVersion.id).where(ToolVersion.tool_id == item.id)).all()]
        if version_ids:
            # 级联：执行日志 → skill 绑定 → 连接器链接 → 版本（Confirmation 版本外键为 SET NULL，由数据库自动置空）。
            # 绑定行随删除级联清理；运行时守卫会跳过已归档资源，故不影响 agent 执行安全。
            db.query(SkillExecutionLog).filter(SkillExecutionLog.tool_version_id.in_(version_ids)).delete(synchronize_session=False)
            db.query(SkillVersionTool).filter(SkillVersionTool.tool_version_id.in_(version_ids)).delete(synchronize_session=False)
            db.query(ConnectorTool).filter(ConnectorTool.tool_id == item.id).delete(synchronize_session=False)
            db.query(ToolVersion).filter(ToolVersion.tool_id == item.id).delete(synchronize_session=False)
        record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="TOOL_DELETED", resource_type="TOOL", resource_id=item.id, summary="tool deleted")
    else:
        version_ids = [v for v in db.scalars(select(SkillVersion.id).where(SkillVersion.skill_id == item.id)).all()]
        if version_ids:
            # 级联：执行日志 → 工具绑定 → agent 绑定 → 版本。绑定行随删除级联清理，不影响版本快照内容。
            db.query(SkillExecutionLog).filter(SkillExecutionLog.skill_version_id.in_(version_ids)).delete(synchronize_session=False)
            db.query(SkillVersionTool).filter(SkillVersionTool.skill_version_id.in_(version_ids)).delete(synchronize_session=False)
            db.query(AgentVersionSkill).filter(AgentVersionSkill.skill_version_id.in_(version_ids)).delete(synchronize_session=False)
            db.query(SkillVersion).filter(SkillVersion.skill_id == item.id).delete(synchronize_session=False)
        record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="SKILL_DELETED", resource_type="SKILL", resource_id=item.id, summary="skill deleted")
    db.delete(item)


def create_tool_version(db: Session, *, tenant_id: UUID, user_id: UUID, tool_id: UUID, config_snapshot: dict, input_schema: dict, output_schema: dict | None, required_permissions: list[str], requires_confirmation: bool, timeout_seconds: float, max_retries: int, retry_backoff_seconds: float) -> ToolVersion:
    tool = _owned(db, Tool, tenant_id, user_id, tool_id)
    number = (db.scalar(select(func.max(ToolVersion.version_number)).where(ToolVersion.tool_id == tool.id)) or 0) + 1
    item = ToolVersion(tenant_id=tenant_id, tool_id=tool.id, version_number=number, status="DRAFT", config_snapshot=config_snapshot, input_schema=input_schema, output_schema=output_schema, required_permissions=required_permissions, requires_confirmation=requires_confirmation, timeout_seconds=timeout_seconds, max_retries=max_retries, retry_backoff_seconds=retry_backoff_seconds)
    db.add(item); db.flush(); return item


def publish_tool_version(db: Session, *, tenant_id: UUID, user_id: UUID, version_id: UUID) -> ToolVersion:
    item = db.get(ToolVersion, version_id)
    if not item or item.tenant_id != tenant_id: raise ApiError("AI_TOOL_VERSION_NOT_FOUND", "tool version not found", 404)
    tool = _owned(db, Tool, tenant_id, user_id, item.tool_id)
    if item.status != "DRAFT": raise ApiError("AI_REQUEST_INVALID", "version is not a draft", 422)
    item.status = "PUBLISHED"; item.snapshot_hash = _snapshot_hash(item.config_snapshot); item.published_at = datetime.now(timezone.utc); tool.current_published_version_id = item.id
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="TOOL_VERSION_PUBLISHED", resource_type="TOOL_VERSION", resource_id=item.id, summary="tool version published")
    return item


def create_skill_version(db: Session, *, tenant_id: UUID, user_id: UUID, skill_id: UUID, instructions: str | None, script_source: str | None, required_permissions: list[str], requires_confirmation: bool, orchestration_hint: str | None = None) -> SkillVersion:
    skill = _owned(db, Skill, tenant_id, user_id, skill_id)
    number = (db.scalar(select(func.max(SkillVersion.version_number)).where(SkillVersion.skill_id == skill.id)) or 0) + 1
    item = SkillVersion(tenant_id=tenant_id, skill_id=skill.id, version_number=number, status="DRAFT", config_snapshot={}, instructions=instructions, orchestration_hint=orchestration_hint, script_source=script_source, required_permissions=required_permissions, requires_confirmation=requires_confirmation)
    db.add(item); db.flush(); return item


def _locked_draft_skill_version(db: Session, *, tenant_id: UUID, user_id: UUID, skill_version_id: UUID) -> SkillVersion:
    skill = db.get(SkillVersion, skill_version_id)
    if not skill or skill.tenant_id != tenant_id or skill.status != "DRAFT":
        raise ApiError("AI_REQUEST_INVALID", "skill version is not editable", 422)
    _owned(db, Skill, tenant_id, user_id, skill.skill_id)
    return skill


def attach_tool(db: Session, *, tenant_id: UUID, user_id: UUID, skill_version_id: UUID, tool_version_id: UUID, sort_order: int | None = None, role: str | None = None, note: str | None = None) -> SkillVersionTool:
    skill = _locked_draft_skill_version(db, tenant_id=tenant_id, user_id=user_id, skill_version_id=skill_version_id)
    tool = db.get(ToolVersion, tool_version_id)
    if not tool or tool.tenant_id != tenant_id or tool.status != "PUBLISHED": raise ApiError("AI_TOOL_VERSION_NOT_FOUND", "published tool version not found", 404)
    exists = db.scalar(select(SkillVersionTool).where(SkillVersionTool.skill_version_id == skill.id, SkillVersionTool.tool_version_id == tool.id))
    if exists: raise ApiError("AI_REQUEST_INVALID", "tool version already attached", 422)
    count = db.scalar(select(func.count(SkillVersionTool.id)).where(SkillVersionTool.skill_version_id == skill.id)) or 0
    if count >= MAX_SKILL_BOUND_TOOLS:
        raise ApiError("AI_SKILL_TOOL_LIMIT_EXCEEDED", f"a skill version can bind at most {MAX_SKILL_BOUND_TOOLS} tools", 409)
    if sort_order is None:
        max_order = db.scalar(select(func.max(SkillVersionTool.sort_order)).where(SkillVersionTool.skill_version_id == skill.id))
        sort_order = (max_order if max_order is not None else -1) + 1
    link = SkillVersionTool(tenant_id=tenant_id, skill_version_id=skill.id, tool_version_id=tool.id, sort_order=sort_order, role=validate_binding_role(role), note=(note.strip() or None) if note is not None else None)
    db.add(link); db.flush(); return link


def detach_tool(db: Session, *, tenant_id: UUID, user_id: UUID, skill_version_id: UUID, tool_version_id: UUID) -> SkillVersionTool:
    skill = _locked_draft_skill_version(db, tenant_id=tenant_id, user_id=user_id, skill_version_id=skill_version_id)
    link = db.scalar(select(SkillVersionTool).where(SkillVersionTool.skill_version_id == skill.id, SkillVersionTool.tool_version_id == tool_version_id))
    if not link: raise ApiError("AI_RESOURCE_NOT_FOUND", "tool link not found", 404)
    db.delete(link); db.flush(); return link


def update_tool_binding(db: Session, *, tenant_id: UUID, user_id: UUID, skill_version_id: UUID, tool_version_id: UUID, sort_order: int | None = None, role: str | None = None, note: str | None = None) -> SkillVersionTool:
    skill = _locked_draft_skill_version(db, tenant_id=tenant_id, user_id=user_id, skill_version_id=skill_version_id)
    link = db.scalar(select(SkillVersionTool).where(SkillVersionTool.skill_version_id == skill.id, SkillVersionTool.tool_version_id == tool_version_id))
    if not link: raise ApiError("AI_RESOURCE_NOT_FOUND", "tool link not found", 404)
    if sort_order is not None:
        link.sort_order = sort_order
    if role is not None:
        link.role = validate_binding_role(role)
    if note is not None:
        link.note = note.strip() or None
    db.flush(); return link


def reorder_tool_bindings(db: Session, *, tenant_id: UUID, user_id: UUID, skill_version_id: UUID, tool_version_ids: list[UUID]) -> list[SkillVersionTool]:
    skill = _locked_draft_skill_version(db, tenant_id=tenant_id, user_id=user_id, skill_version_id=skill_version_id)
    links = _binding_links(db, skill.id)
    current = [str(link.tool_version_id) for link in links]
    wanted = [str(item) for item in tool_version_ids]
    if len(wanted) != len(set(wanted)) or sorted(wanted) != sorted(current):
        raise ApiError("AI_REQUEST_INVALID", "tool binding order must cover the current bindings exactly", 422)
    by_id = {str(link.tool_version_id): link for link in links}
    ordered = [by_id[item] for item in wanted]
    for index, link in enumerate(ordered):
        link.sort_order = index
    db.flush(); return ordered


def upgrade_tool_binding(db: Session, *, tenant_id: UUID, user_id: UUID, skill_version_id: UUID, tool_version_id: UUID, new_tool_version_id: UUID) -> SkillVersionTool:
    """把绑定升级到同一工具的新发布版本：保留 sort_order / role / note，仅草稿版本可操作。"""
    skill = _locked_draft_skill_version(db, tenant_id=tenant_id, user_id=user_id, skill_version_id=skill_version_id)
    link = db.scalar(select(SkillVersionTool).where(SkillVersionTool.skill_version_id == skill.id, SkillVersionTool.tool_version_id == tool_version_id))
    if not link:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "tool link not found", 404)
    if new_tool_version_id == tool_version_id:
        raise ApiError("AI_REQUEST_INVALID", "binding already points to this version", 422)
    current_version = db.get(ToolVersion, tool_version_id)
    new_version = db.get(ToolVersion, new_tool_version_id)
    if not current_version or not new_version or new_version.tenant_id != tenant_id:
        raise ApiError("AI_TOOL_VERSION_NOT_FOUND", "tool version not found", 404)
    if new_version.tool_id != current_version.tool_id:
        raise ApiError("AI_REQUEST_INVALID", "can only upgrade within the same tool", 422)
    if new_version.status != "PUBLISHED":
        raise ApiError("AI_REQUEST_INVALID", "target tool version is not published", 422)
    exists = db.scalar(select(SkillVersionTool).where(SkillVersionTool.skill_version_id == skill.id, SkillVersionTool.tool_version_id == new_version.id))
    if exists:
        raise ApiError("AI_REQUEST_INVALID", "tool version already attached", 422)
    link.tool_version_id = new_version.id
    db.flush()
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="SKILL_TOOL_BINDING_UPGRADED", resource_type="SKILL_VERSION", resource_id=skill.id, summary="tool binding upgraded", detail={"from_tool_version_id": str(tool_version_id), "to_tool_version_id": str(new_version.id)})
    return link


def publish_skill_version(db: Session, *, tenant_id: UUID, user_id: UUID, version_id: UUID) -> SkillVersion:
    item = db.get(SkillVersion, version_id)
    if not item or item.tenant_id != tenant_id: raise ApiError("AI_SKILL_VERSION_NOT_FOUND", "skill version not found", 404)
    _owned(db, Skill, tenant_id, user_id, item.skill_id)
    if item.status != "DRAFT": raise ApiError("AI_REQUEST_INVALID", "version is not a draft", 422)
    if item.config_snapshot.get("implementation") == "md_script":
        if not item.config_snapshot.get("allow_scripts") or not item.config_snapshot.get("script_files"):
            raise ApiError("AI_SKILL_INVALID_MANIFEST", "MD-Skill script version is incomplete", 422)
    if item.config_snapshot.get("implementation") == "md_instruction" and item.script_source:
        raise ApiError("AI_SKILL_INVALID_MANIFEST", "instruction skill cannot contain script source", 422)
    # 绑定元数据（顺序/角色/备注）一并进入快照哈希，发布后不可原地篡改
    bindings = [
        {"tool_version_id": str(x.tool_version_id), "sort_order": x.sort_order, "role": x.role, "note": x.note}
        for x in _binding_links(db, item.id)
    ]
    if len(bindings) > MAX_SKILL_BOUND_TOOLS:
        raise ApiError("AI_SKILL_TOOL_LIMIT_EXCEEDED", f"a skill version can bind at most {MAX_SKILL_BOUND_TOOLS} tools", 409)
    item.status = "PUBLISHED"; item.snapshot_hash = _snapshot_hash({"config": item.config_snapshot, "instructions": item.instructions, "orchestration_hint": item.orchestration_hint, "tool_bindings": bindings}); item.published_at = datetime.now(timezone.utc)
    db.query(Skill).filter(Skill.id == item.skill_id).update({Skill.current_published_version_id: item.id})
    return item


def update_skill_version(db: Session, *, tenant_id: UUID, user_id: UUID, version_id: UUID, instructions: str | None = None, orchestration_hint: str | None = None, script_source: str | None = None, required_permissions: list[str] | None = None, requires_confirmation: bool | None = None) -> SkillVersion:
    item = db.get(SkillVersion, version_id)
    if not item or item.tenant_id != tenant_id:
        raise ApiError("AI_SKILL_VERSION_NOT_FOUND", "skill version not found", 404)
    _owned(db, Skill, tenant_id, user_id, item.skill_id)
    if item.status != "DRAFT":
        raise ApiError("AI_REQUEST_INVALID", "published skill version is immutable", 422)
    if instructions is not None:
        item.instructions = instructions
    if orchestration_hint is not None:
        item.orchestration_hint = orchestration_hint
    if script_source is not None:
        item.script_source = script_source
    if required_permissions is not None:
        item.required_permissions = required_permissions
    if requires_confirmation is not None:
        item.requires_confirmation = requires_confirmation
    return item
