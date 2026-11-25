import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.limits import MAX_SKILL_BOUND_TOOLS
from app.kernel.audit.service import record_audit
from app.kernel.task.states import TERMINAL_STATES
from app.models import Agent, AgentVersion, AgentVersionSkill, Skill, SkillVersion, SkillVersionTool, Task, Tool, ToolVersion

VERSION_NOT_FOUND = "version not found"

# 归档阻断错误码（前端按 code 映射中文文案）
ARCHIVE_ERR_CURRENT = "AI_AGENT_VERSION_IS_CURRENT"
ARCHIVE_ERR_IN_USE = "AI_AGENT_VERSION_IN_USE"


def _snapshot_hash(snapshot: dict, *, skill_version_ids: list[UUID] | None = None, tool_version_ids: set[str] | None = None) -> str:
    """Hash the executable version contract, including immutable dependency references."""
    payload = {
        "config_snapshot": snapshot,
        "skill_version_ids": sorted(str(item) for item in (skill_version_ids or [])),
        "tool_version_ids": sorted(str(item) for item in (tool_version_ids or set())),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _owned_agent(db: Session, tenant_id: UUID, user_id: UUID, agent_id: UUID) -> Agent:
    agent = db.scalar(select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id, Agent.owner_user_id == user_id, Agent.source_type == "USER", Agent.status == "ACTIVE"))
    if not agent:
        raise ApiError("AI_AGENT_NOT_FOUND", "agent not found", 404)
    return agent


def create_version(db: Session, *, tenant_id: UUID, user_id: UUID, agent_id: UUID, snapshot: dict) -> AgentVersion:
    _owned_agent(db, tenant_id, user_id, agent_id)
    number = (db.scalar(select(func.max(AgentVersion.version_number)).where(AgentVersion.agent_id == agent_id)) or 0) + 1
    version = AgentVersion(
        tenant_id=tenant_id,
        agent_id=agent_id,
        version_number=number,
        status="DRAFT",
        config_schema_version="1",
        config_snapshot=snapshot,
    )
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
    # delegation_updates is intentionally ignored. Legacy columns remain for historical compatibility.
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
    bound_skill_ids: list[UUID] = []
    for link in db.query(AgentVersionSkill).filter(AgentVersionSkill.agent_version_id == version.id).all():
        skill = db.get(SkillVersion, link.skill_version_id)
        if not skill or skill.tenant_id != tenant_id or skill.status != "PUBLISHED":
            raise ApiError("AI_AGENT_SKILL_VERSION_NOT_PUBLISHED", "agent version references an unpublished skill version", 422)
        bound_skill_ids.append(skill.id)
    # 双闸门之二：AgentVersion 发布时按去重后的工具版本数限制模型侧暴露规模
    exposed_tool_ids: set[str] = set()
    if bound_skill_ids:
        rows = db.scalars(
            select(SkillVersionTool.tool_version_id).where(SkillVersionTool.skill_version_id.in_(bound_skill_ids))
        ).all()
        exposed_tool_ids = {str(item) for item in rows}
    if len(exposed_tool_ids) > MAX_SKILL_BOUND_TOOLS:
        raise ApiError(
            "AI_AGENT_TOOL_LIMIT_EXCEEDED",
            f"an agent version can expose at most {MAX_SKILL_BOUND_TOOLS} tools, got {len(exposed_tool_ids)}",
            409,
        )
    version.status = "PUBLISHED"
    version.snapshot_hash = _snapshot_hash(version.config_snapshot, skill_version_ids=bound_skill_ids, tool_version_ids=exposed_tool_ids)
    version.published_at = datetime.now(timezone.utc)
    agent.current_published_version_id = version.id
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="AGENT_VERSION_PUBLISHED", resource_type="AGENT_VERSION", resource_id=version.id, summary="agent version published")
    return version


def _running_task_count(db: Session, tenant_id: UUID, version_id: UUID) -> int:
    """归档保护：统计仍引用该不可变版本快照的非终态任务。"""
    return (
        db.scalar(
            select(func.count(Task.id)).where(
                Task.tenant_id == tenant_id,
                Task.agent_version_id == version_id,
                Task.status.notin_([str(status) for status in TERMINAL_STATES]),
            )
        )
        or 0
    )


def _archive_blockers(db: Session, agent: Agent, version: AgentVersion) -> tuple[list[dict], list[dict], int, list[AgentVersion]]:
    """归档阻断项与提示项：只检查当前发布指针和非终态 Task 引用。

    A2A 目标由运行时 Discovery 解析，不形成 AgentVersion 静态引用关系。
    """

    reasons: list[dict] = []
    warnings: list[dict] = []
    running = _running_task_count(db, version.tenant_id, version.id)
    referrers: list[AgentVersion] = []  # Dynamic A2A discovery creates no static version references

    if version.id == agent.current_published_version_id:
        reasons.append({"code": ARCHIVE_ERR_CURRENT, "message": "当前发布版本不可归档，请先发布其他版本"})
    if running:
        reasons.append({"code": ARCHIVE_ERR_IN_USE, "message": f"该版本有 {running} 个任务正在执行"})
    return reasons, warnings, running, referrers


def archive_check(db: Session, *, tenant_id: UUID, user_id: UUID, version_id: UUID) -> dict:
    """归档预检：前端点击归档前调用，用于给出精确的阻断原因（与 archive_version 同源判定）。"""
    version = db.get(AgentVersion, version_id)
    if not version or version.tenant_id != tenant_id:
        raise ApiError("AI_AGENT_VERSION_NOT_FOUND", VERSION_NOT_FOUND, 404)
    agent = _owned_agent(db, tenant_id, user_id, version.agent_id)
    reasons, warnings, running, referrers = _archive_blockers(db, agent, version)
    return {
        "can_archive": not reasons,
        "already_archived": version.status == "ARCHIVED",
        "reasons": reasons,
        "warnings": warnings,
        "running_task_count": running,
        "referenced_by": [],
    }


def archive_version(db: Session, *, tenant_id: UUID, user_id: UUID, version_id: UUID) -> AgentVersion:
    version = db.get(AgentVersion, version_id)
    if not version or version.tenant_id != tenant_id:
        raise ApiError("AI_AGENT_VERSION_NOT_FOUND", VERSION_NOT_FOUND, 404)
    agent = _owned_agent(db, tenant_id, user_id, version.agent_id)
    # 幂等：已归档直接返回，重复点击/重试不报错
    if version.status == "ARCHIVED":
        return version
    reasons, _warnings, _running, _referrers = _archive_blockers(db, agent, version)
    if reasons:
        raise ApiError(reasons[0]["code"], "；".join(r["message"] for r in reasons), 409)
    version.status = "ARCHIVED"
    record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        action="AGENT_VERSION_ARCHIVED",
        resource_type="AGENT_VERSION",
        resource_id=version.id,
        summary="agent version archived",
        detail={"agent_id": str(agent.id), "version_number": version.version_number},
    )
    return version


def rollback_version(db: Session, *, tenant_id: UUID, user_id: UUID, version_id: UUID) -> AgentVersion:
    """切换当前发布指针到历史快照；不修改 AgentVersion 配置内容。"""
    version = db.get(AgentVersion, version_id)
    if version is None or version.tenant_id != tenant_id:
        raise ApiError("AI_AGENT_VERSION_NOT_FOUND", VERSION_NOT_FOUND, 404)
    agent = _owned_agent(db, tenant_id, user_id, version.agent_id)
    if version.status not in {"PUBLISHED", "ARCHIVED"}:
        raise ApiError("AI_AGENT_VERSION_NOT_ROLLBACKABLE", "only published or archived versions can be rolled back", 409)
    if agent.current_published_version_id == version.id:
        return version

    links = db.scalars(select(AgentVersionSkill).where(
        AgentVersionSkill.tenant_id == tenant_id,
        AgentVersionSkill.agent_version_id == version.id,
    )).all()
    for link in links:
        skill_version = db.get(SkillVersion, link.skill_version_id)
        skill = db.get(Skill, skill_version.skill_id) if skill_version is not None else None
        if (
            skill_version is None or skill_version.tenant_id != tenant_id
            or skill_version.status != "PUBLISHED" or skill is None or skill.status != "ACTIVE"
        ):
            raise ApiError("AI_AGENT_VERSION_DEPENDENCY_UNAVAILABLE", "a pinned Skill version required by this Agent version is no longer available", 409)
        tool_ids = db.scalars(select(SkillVersionTool.tool_version_id).where(
            SkillVersionTool.tenant_id == tenant_id,
            SkillVersionTool.skill_version_id == skill_version.id,
        )).all()
        for tool_id in tool_ids:
            tool_version = db.get(ToolVersion, tool_id)
            tool = db.get(Tool, tool_version.tool_id) if tool_version is not None else None
            if (
                tool_version is None or tool_version.tenant_id != tenant_id
                or tool_version.status != "PUBLISHED" or tool is None or tool.status != "ACTIVE"
            ):
                raise ApiError("AI_AGENT_VERSION_DEPENDENCY_UNAVAILABLE", "a pinned Tool version required by this Agent version is no longer available", 409)

    previous_version_id = agent.current_published_version_id
    version.status = "PUBLISHED"
    agent.current_published_version_id = version.id
    record_audit(
        db, tenant_id=tenant_id, actor_user_id=user_id,
        action="AGENT_VERSION_ROLLED_BACK", resource_type="AGENT_VERSION",
        resource_id=version.id, summary="agent current published version rolled back",
        detail={"agent_id": str(agent.id), "version_number": version.version_number,
                "previous_version_id": str(previous_version_id) if previous_version_id else None},
    )
    return version
