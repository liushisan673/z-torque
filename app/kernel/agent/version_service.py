from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.limits import MAX_SKILL_BOUND_TOOLS
from app.models import Agent, AgentVersion, AgentVersionSkill, SkillVersion, SkillVersionTool, Task
from app.kernel.audit.service import record_audit
from app.kernel.task.states import TERMINAL_STATES

VERSION_NOT_FOUND = "version not found"

# 归档阻断错误码（前端按 code 映射中文文案）
ARCHIVE_ERR_CURRENT = "AI_AGENT_VERSION_IS_CURRENT"
ARCHIVE_ERR_IN_USE = "AI_AGENT_VERSION_IN_USE"
ARCHIVE_ERR_REFERENCED = "AI_AGENT_VERSION_REFERENCED"
ARCHIVE_WARN_REFERENCED_BY_DRAFT = "AI_AGENT_VERSION_REFERENCED_BY_DRAFT"


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
    # 委派配置随新草稿继承当前发布版本（启用开关 / 任务数上限 / 委派白名单），
    # 避免新建草稿后被清空重填；无发布版本时保持默认（不启用）
    current = db.scalar(
        select(AgentVersion)
        .where(AgentVersion.agent_id == agent_id, AgentVersion.status == "PUBLISHED")
        .order_by(AgentVersion.version_number.desc())
    )
    version = AgentVersion(
        tenant_id=tenant_id,
        agent_id=agent_id,
        version_number=number,
        status="DRAFT",
        config_schema_version="1",
        config_snapshot=snapshot,
        delegation_enabled=bool(current.delegation_enabled) if current is not None else False,
        allowed_child_agent_versions=list(current.allowed_child_agent_versions or []) if current is not None else [],
        child_necessity_policy=dict(current.child_necessity_policy) if current is not None and current.child_necessity_policy else None,
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
    bound_skill_ids: list[UUID] = []
    for link in db.query(AgentVersionSkill).filter(AgentVersionSkill.agent_version_id == version.id).all():
        skill = db.get(SkillVersion, link.skill_version_id)
        if not skill or skill.tenant_id != tenant_id or skill.status != "PUBLISHED":
            raise ApiError("AI_REQUEST_INVALID", "agent version references an unpublished skill version", 422)
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


def _referrer_agent_name(db: Session, version: AgentVersion) -> str:
    agent = db.get(Agent, version.agent_id)
    return agent.name if agent else "未知智能体"


def _version_label(db: Session, version: AgentVersion) -> str:
    return f"{_referrer_agent_name(db, version)} v{version.version_number}"


def _running_task_count(db: Session, tenant_id: UUID, version_id: UUID) -> int:
    """引用该版本且未终态的任务数（含委派子任务）：运行中 = 不允许归档。"""
    return (
        db.scalar(
            select(func.count(Task.id)).where(
                Task.tenant_id == tenant_id,
                Task.agent_version_id == version_id,
                Task.status.notin_([str(s) for s in TERMINAL_STATES]),
            )
        )
        or 0
    )


def _delegation_referrers(db: Session, tenant_id: UUID, version_id: UUID) -> list[AgentVersion]:
    """其他版本中把该版本列入委派白名单的引用方（DRAFT / PUBLISHED）。"""
    target = str(version_id)
    rows = db.scalars(
        select(AgentVersion).where(
            AgentVersion.tenant_id == tenant_id,
            AgentVersion.id != version_id,
            AgentVersion.delegation_enabled.is_(True),
            AgentVersion.status.in_(["DRAFT", "PUBLISHED"]),
        )
    ).all()
    return [v for v in rows if target in [str(x) for x in (v.allowed_child_agent_versions or [])]]


def _archive_blockers(db: Session, agent: Agent, version: AgentVersion) -> tuple[list[dict], list[dict], int, list[AgentVersion]]:
    """归档阻断项与提示项：返回 (reasons, warnings, running_task_count, referrers)。

    reasons 非空即不可归档；warnings 仅提示不阻断。
    """
    reasons: list[dict] = []
    warnings: list[dict] = []
    running = _running_task_count(db, version.tenant_id, version.id)
    referrers = _delegation_referrers(db, version.tenant_id, version.id)

    if version.id == agent.current_published_version_id:
        reasons.append({"code": ARCHIVE_ERR_CURRENT, "message": "当前发布版本不可归档，请先发布其他版本"})
    if running:
        reasons.append({"code": ARCHIVE_ERR_IN_USE, "message": f"该版本有 {running} 个任务正在执行"})
    published_refs = [v for v in referrers if v.status == "PUBLISHED"]
    if published_refs:
        shown = "、".join(_version_label(db, v) for v in published_refs[:3])
        reasons.append({"code": ARCHIVE_ERR_REFERENCED, "message": f"该版本被已发布版本（{shown}{'等' if len(published_refs) > 3 else ''}）的委派白名单引用"})
    draft_refs = [v for v in referrers if v.status != "PUBLISHED"]
    if draft_refs:
        shown = "、".join(_version_label(db, v) for v in draft_refs[:3])
        warnings.append({"code": ARCHIVE_WARN_REFERENCED_BY_DRAFT, "message": f"草稿（{shown}{'等' if len(draft_refs) > 3 else ''}）的委派白名单引用了本版本，归档后其发布会失败"})
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
        "referenced_by": [
            {
                "version_id": str(v.id),
                "agent_name": _referrer_agent_name(db, v),
                "version_number": v.version_number,
                "status": v.status,
            }
            for v in referrers
        ],
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