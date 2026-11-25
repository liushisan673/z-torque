from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.kernel.audit.service import record_audit
from app.models import Agent, AgentVersion, AgentVersionSkill, Conversation, ShortTermMemory


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
    # 归档的智能体不再作为默认项（外部接入 fallback 会跳过非 ACTIVE 的默认项，这里直接清标记）
    agent.is_default = False
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


def set_default_agent(db: Session, *, tenant_id: UUID, user_id: UUID, agent_id: UUID) -> Agent:
    """M12-4：设为用户默认智能体（同租户互斥，设置新默认时清掉旧的）。

    与 set_default_parent 不同：不限定 OFFICIAL，任何可见且 ACTIVE 的智能体均可设为默认；
    是否已发布不做硬校验（未发布的默认项在会话页 fallback 中会被自然跳过）。
    """
    agent = _agent(db, tenant_id, user_id, agent_id)
    if agent.status != "ACTIVE":
        raise ApiError("AI_REQUEST_INVALID", "default agent must be active", 422)
    db.query(Agent).filter(Agent.tenant_id == tenant_id, Agent.is_default.is_(True), Agent.id != agent.id).update(
        {Agent.is_default: False}, synchronize_session="fetch"
    )
    agent.is_default = True
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="AGENT_DEFAULT_SET", resource_type="AGENT", resource_id=agent.id, summary="default agent set")
    return agent


def set_default_parent(db: Session, *, tenant_id: UUID, user_id: UUID, agent_id: UUID) -> Agent:
    agent = _agent(db, tenant_id, user_id, agent_id)
    if agent.source_type != "OFFICIAL" or agent.owner_user_id is not None:
        raise ApiError("AI_FORBIDDEN", "only official agents can be the default parent", 403)
    if agent.status != "ACTIVE" or agent.current_published_version_id is None:
        raise ApiError("AI_REQUEST_INVALID", "default parent requires an active published agent", 422)
    # 仅在本租户内互斥（契约 §1：任何数据访问必须带租户条件）；
    # 缺少 tenant_id 会把其他租户的默认父 Agent 一并置 false。
    db.query(Agent).filter(Agent.tenant_id == tenant_id, Agent.is_default_parent.is_(True), Agent.id != agent.id).update(
        {Agent.is_default_parent: False}, synchronize_session="fetch"
    )
    agent.is_default_parent = True
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="DEFAULT_PARENT_SET", resource_type="AGENT", resource_id=agent.id, summary="default parent set")
    return agent


SYSTEM_AGENT_NAME = "Z-Torque 系统智能体"
SYSTEM_AGENT_DESCRIPTION = "系统内置智能体：自动识别需求并派发给合适的智能体，无需用户手动选择。"
SYSTEM_AGENT_SNAPSHOT = {
    "mode": "router",
    "system_prompt": "你是 Z-Torque 系统智能体。先判断是否需要专业 Agent；需要时通过系统提供的唯一结构化 Agent 路由工具从候选中选择最匹配的一位，并提交完整任务描述。简单闲聊和通用问题直接回答。",
}


def ensure_system_agent(db: Session, *, tenant_id: UUID) -> Agent:
    """系统自带智能体：每个租户启动时确保存在且已发布，用户配置模型后即可用。

    旧安装的默认父可能是早期 bootstrap 的 `mode=mock` 空壳：这里就地升级其名称/描述，
    并为其已发布版本补上 router 系统提示快照（不新建版本，避免影响历史任务的版本引用）。
    """
    agent = bootstrap_default_parent(
        db, tenant_id=tenant_id, owner_user_id=None,
        name=SYSTEM_AGENT_NAME, description=SYSTEM_AGENT_DESCRIPTION,
        snapshot=SYSTEM_AGENT_SNAPSHOT,
    )
    agent.name = SYSTEM_AGENT_NAME
    agent.description = SYSTEM_AGENT_DESCRIPTION
    if agent.current_published_version_id is not None:
        version = db.get(AgentVersion, agent.current_published_version_id)
        if version is not None and version.config_snapshot != SYSTEM_AGENT_SNAPSHOT:
            from app.kernel.agent.version_service import _snapshot_hash
            version.config_snapshot = dict(SYSTEM_AGENT_SNAPSHOT)
            version.snapshot_hash = _snapshot_hash(version.config_snapshot)
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