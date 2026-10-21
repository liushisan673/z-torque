import re
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.ids import new_uuid7
from app.kernel.registry.active import emit_event
from app.kernel.security_context import SecurityContext
from app.kernel.task.states import TERMINAL_STATES, TaskStatus, can_transition
from app.models import Agent, AgentVersion, Conversation, Message, Outbox, Task, TaskEvent


def _now() -> datetime:
    return datetime.now(timezone.utc)


_REDACTED = "[REDACTED]"
# 敏感词表：既用于"键=值"整体脱敏，也用于裸词兜底
_SENSITIVE_WORDS = r"(?:password|passwd|pwd|token|secret|api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key)"
# 键值对：password=xxx / "token": "xxx" / authorization: xxx → 键与值整体替换
# （仅替换键名会让明文口令落库；键名可能被 JSON/查询串引号包裹，故允许两侧成对引号）
_SENSITIVE_PAIR = re.compile(
    r"(?i)\b" + _SENSITIVE_WORDS + r"\b[\"']?\s*[:=：]\s*(?:\[REDACTED\]|[\"']?[^\s,;&)\]>\"']+[\"']?)"
)
# Bearer/Basic 等认证头形态：Authorization: Bearer xxx → 凭证替换
_SENSITIVE_CREDENTIAL = re.compile(r"(?i)\b(bearer|basic|digest)\s+([A-Za-z0-9\-._~+/=]+)")
# 兜底：未带值的裸敏感词（如 "token expired"），与旧行为保持一致
_SENSITIVE_BARE = re.compile(r"(?i)\b" + _SENSITIVE_WORDS + r"\b(?!\s*[:=：])")
_MAX_SUMMARY_LENGTH = 2000


def sanitize_summary(value: str | None) -> str:
    """任务摘要脱敏：键值对整体脱敏，避免只替换键名而把明文口令/密钥落库。

    顺序为「认证凭证 → 键值对 → 裸敏感词」：先处理 Bearer 等凭证形态，
    再连同键名一起抹掉（仅保留 [REDACTED]），避免键名或值任一残留。
    """
    if not value:
        return ""
    text = value[:_MAX_SUMMARY_LENGTH]
    text = _SENSITIVE_CREDENTIAL.sub(_REDACTED, text)
    text = _SENSITIVE_PAIR.sub(_REDACTED, text)
    return _SENSITIVE_BARE.sub(_REDACTED, text)


def _resolve_trigger_message(trigger_message_id: UUID | None, message_id: UUID | None) -> UUID | None:
    if trigger_message_id and message_id and trigger_message_id != message_id:
        raise ApiError("AI_REQUEST_INVALID", "trigger and legacy message references differ", 422)
    return trigger_message_id or message_id


def _visible_agent(db: Session, *, tenant_id: UUID, owner_user_id: UUID, agent_id: UUID, require_published: bool = False) -> Agent | None:
    """按 chat 一致的可见性口径（本人 | OFFICIAL | 本租户管理员创建）取 Agent。"""
    from app.security.permissions import admin_owned_clause

    stmt = select(Agent).where(
        Agent.id == agent_id,
        Agent.tenant_id == tenant_id,
        Agent.status == "ACTIVE",
        (Agent.owner_user_id == owner_user_id) | (Agent.source_type == "OFFICIAL") | admin_owned_clause(Agent.owner_user_id, tenant_id),
    )
    if require_published:
        stmt = stmt.where(Agent.current_published_version_id.is_not(None))
    return db.scalar(stmt)


def assert_agent_version_visible(db: Session, *, tenant_id: UUID, owner_user_id: UUID, agent_id: UUID, agent_version_id: UUID) -> None:
    """接口层强校验（契约 §1 + 阶段2文档 4.1）：Agent 可见且版本已发布、归属一致。"""
    if _visible_agent(db, tenant_id=tenant_id, owner_user_id=owner_user_id, agent_id=agent_id, require_published=True) is None:
        raise ApiError("AI_AGENT_NOT_FOUND", "agent not found", 404)
    version = db.scalar(select(AgentVersion).where(AgentVersion.id == agent_version_id, AgentVersion.agent_id == agent_id, AgentVersion.tenant_id == tenant_id, AgentVersion.status == "PUBLISHED"))
    if version is None:
        raise ApiError("AI_AGENT_VERSION_NOT_PUBLISHED", "agent version is not published", 409)


def _validate_stage3_context(
    db: Session,
    *,
    tenant_id: UUID,
    owner_user_id: UUID,
    agent_id: UUID,
    agent_version_id: UUID,
    conversation_id: UUID | None,
    trigger_message_id: UUID | None,
) -> Message | None:
    agent = _visible_agent(db, tenant_id=tenant_id, owner_user_id=owner_user_id, agent_id=agent_id)
    version = db.scalar(select(AgentVersion).where(AgentVersion.id == agent_version_id, AgentVersion.agent_id == agent_id, AgentVersion.tenant_id == tenant_id, AgentVersion.status == "PUBLISHED"))
    if conversation_id is not None or trigger_message_id is not None:
        if agent is None:
            raise ApiError("AI_AGENT_NOT_FOUND", "agent not found", 404)
        if version is None:
            raise ApiError("AI_AGENT_VERSION_NOT_PUBLISHED", "agent version is not published", 409)
    else:
        # 无会话上下文的直接创建：内核保持阶段2的宽松语义（允许引用尚未落库的测试标识），
        # 但对已存在却不可见/跨租户的快照必须拒绝（P1-5），
        # 面向不可信输入的完整校验由接口层 assert_agent_version_visible 负责。
        if agent is None and db.scalar(select(Agent.id).where(Agent.id == agent_id)) is not None:
            raise ApiError("AI_AGENT_NOT_FOUND", "agent not found", 404)
        if version is None and db.scalar(select(AgentVersion.id).where(AgentVersion.id == agent_version_id)) is not None:
            raise ApiError("AI_AGENT_VERSION_NOT_PUBLISHED", "agent version is not published", 409)
    if conversation_id is not None:
        _validate_conversation(db, tenant_id, owner_user_id, agent_id, conversation_id)
    if trigger_message_id is None:
        return None
    message = db.scalar(select(Message).where(Message.id == trigger_message_id, Message.tenant_id == tenant_id, Message.conversation_id == conversation_id, Message.sender_type == "USER"))
    if message is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "trigger message not found", 404)
    if message.task_id is not None:
        raise ApiError("AI_REQUEST_INVALID", "message already has a task", 422)
    return message


def _validate_conversation(db: Session, tenant_id: UUID, owner_user_id: UUID, agent_id: UUID, conversation_id: UUID) -> None:
    conversation = db.scalar(select(Conversation).where(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id, Conversation.owner_user_id == owner_user_id, Conversation.agent_id == agent_id))
    if conversation is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "conversation not found", 404)


def _create_task_records(
    db: Session,
    *,
    tenant_id: UUID,
    owner_user_id: UUID,
    agent_id: UUID,
    agent_version_id: UUID,
    idempotency_key: str,
    request_summary: str,
    parent_task_id: UUID | None,
    delegation_id: UUID | None,
    child_sequence: int | None,
    depends_on_task_ids: list[str] | None,
    deadline_at: datetime | None,
    conversation_id: UUID | None,
    trigger_message_id: UUID | None,
    trace_id: str | None,
) -> Task:
    root_id, depth = _resolve_parent(db, tenant_id, parent_task_id)
    task = Task(id=root_id if parent_task_id is None else new_uuid7(), tenant_id=tenant_id, owner_user_id=owner_user_id, parent_task_id=parent_task_id, root_task_id=root_id, delegation_depth=depth, child_sequence=child_sequence, delegation_id=delegation_id, depends_on_task_ids=depends_on_task_ids, agent_id=agent_id, agent_version_id=agent_version_id, idempotency_key=idempotency_key, request_summary=sanitize_summary(request_summary), deadline_at=deadline_at, conversation_id=conversation_id, trigger_message_id=trigger_message_id, message_id=trigger_message_id)
    db.add(task)
    db.flush()
    task.last_event_seq = 1
    db.add(TaskEvent(id=new_uuid7(), tenant_id=tenant_id, task_id=task.id, seq=1, event_type="TASK_CREATED", to_state=TaskStatus.PENDING, summary=sanitize_summary("task created"), trace_id=trace_id))
    db.add(Outbox(tenant_id=tenant_id, aggregate_type="TASK", aggregate_id=task.id, event_type="TASK_READY", payload={"task_id": str(task.id)}))
    # 注意：trigger 消息不回写 task_id（zr_message.task_id 有 UNIQUE 约束，保留给任务产出的 ASSISTANT 回复）；
    # 任务与触发消息的关联由 Task.trigger_message_id 承载，避免 worker 收尾插入回复时触发唯一约束冲突。
    db.flush()
    return task


def _resolve_parent(db: Session, tenant_id: UUID, parent_task_id: UUID | None) -> tuple[UUID, int]:
    if parent_task_id is None:
        root_id = new_uuid7()
        return root_id, 0
    parent = db.get(Task, parent_task_id)
    if parent is None or parent.tenant_id != tenant_id or parent.root_task_id is None:
        raise ApiError("AI_REQUEST_INVALID", "invalid parent task", 422)
    depth = parent.delegation_depth + 1
    if depth > 1:
        raise ApiError("AI_AGENT_DELEGATION_DEPTH_EXCEEDED", "task delegation depth exceeded", 409)
    return parent.root_task_id, depth


def _validate_delegation(
    db: Session,
    *,
    tenant_id: UUID,
    parent_task_id: UUID | None,
    delegation_id: UUID | None,
    child_sequence: int | None,
) -> None:
    if parent_task_id is None:
        if delegation_id is not None or child_sequence is not None:
            raise ApiError("AI_REQUEST_INVALID", "root tasks cannot have child delegation fields", 422)
        return
    from app.config import get_settings
    settings = get_settings()
    if not settings.delegation_enabled_global:
        raise ApiError("AI_AGENT_DELEGATION_FORBIDDEN", "task delegation is disabled", 403)
    child_count = db.scalar(select(func.count(Task.id)).where(Task.tenant_id == tenant_id, Task.parent_task_id == parent_task_id)) or 0
    if child_count >= settings.delegation_max_children_per_root:
        raise ApiError("AI_AGENT_DELEGATION_LIMIT_EXCEEDED", "maximum child task count exceeded", 429)
    if child_sequence is None or child_sequence < 1:
        raise ApiError("AI_REQUEST_INVALID", "child sequence must start at 1", 422)
    if delegation_id is None:
        raise ApiError("AI_REQUEST_INVALID", "child delegation id is required", 422)
    if db.scalar(select(Task.id).where(Task.tenant_id == tenant_id, Task.parent_task_id == parent_task_id, Task.child_sequence == child_sequence)) is not None:
        raise ApiError("AI_TASK_DUPLICATE_IDEMPOTENCY", "child sequence already exists", 409)
    if db.scalar(select(Task.id).where(Task.tenant_id == tenant_id, Task.delegation_id == delegation_id)) is not None:
        raise ApiError("AI_TASK_DUPLICATE_IDEMPOTENCY", "delegation id already exists", 409)


def _apply_legacy_child_defaults(
    db: Session,
    *,
    tenant_id: UUID,
    parent_task_id: UUID | None,
    delegation_id: UUID | None,
    child_sequence: int | None,
) -> tuple[UUID | None, int | None]:
    if parent_task_id is None:
        return delegation_id, child_sequence
    if child_sequence is None:
        last_sequence = db.scalar(
            select(func.max(Task.child_sequence)).where(
                Task.tenant_id == tenant_id,
                Task.parent_task_id == parent_task_id,
            )
        ) or 0
        child_sequence = last_sequence + 1
    if delegation_id is None:
        delegation_id = new_uuid7()
    return delegation_id, child_sequence


def create_task(
    db: Session,
    *,
    ctx: SecurityContext,
    agent_id: UUID,
    agent_version_id: UUID,
    idempotency_key: str,
    request_summary: str,
    parent_task_id: UUID | None = None,
    delegation_id: UUID | None = None,
    child_sequence: int | None = None,
    depends_on_task_ids: list[str] | None = None,
    deadline_at: datetime | None = None,
    trace_id: str | None = None,
    conversation_id: UUID | None = None,
    trigger_message_id: UUID | None = None,
    message_id: UUID | None = None,
) -> Task:
    """任务创建（不变量1：身份必须以 SecurityContext 进入，冻结为 Task 行 tenant_id/owner_user_id）。"""
    tenant_id = ctx.tenant_id
    owner_user_id = ctx.user_id
    trace_id = trace_id or ctx.trace_id
    trigger_message_id = _resolve_trigger_message(trigger_message_id, message_id)
    if not idempotency_key or len(idempotency_key) > 128:
        raise ApiError("AI_PARAM_INVALID", "idempotency key is too long", 422)
    # 幂等基准：落库值是脱敏+截断后的结果，比较必须用同一变换，否则含敏感词或超长消息的重试会被误判为"不同请求"
    stored_summary = sanitize_summary(request_summary)
    existing = db.query(Task).filter_by(tenant_id=tenant_id, owner_user_id=owner_user_id, idempotency_key=idempotency_key).one_or_none()
    if existing:
        if existing.request_summary != stored_summary or existing.agent_id != agent_id or existing.agent_version_id != agent_version_id:
            raise ApiError("AI_TASK_DUPLICATE_IDEMPOTENCY", "idempotency key was used with different request", 409)
        return existing
    delegation_id, child_sequence = _apply_legacy_child_defaults(
        db,
        tenant_id=tenant_id,
        parent_task_id=parent_task_id,
        delegation_id=delegation_id,
        child_sequence=child_sequence,
    )
    _validate_delegation(db, tenant_id=tenant_id, parent_task_id=parent_task_id, delegation_id=delegation_id, child_sequence=child_sequence)
    _validate_stage3_context(db, tenant_id=tenant_id, owner_user_id=owner_user_id, agent_id=agent_id, agent_version_id=agent_version_id, conversation_id=conversation_id, trigger_message_id=trigger_message_id)
    try:
        with db.begin_nested():
            return _create_task_records(db, tenant_id=tenant_id, owner_user_id=owner_user_id, agent_id=agent_id, agent_version_id=agent_version_id, idempotency_key=idempotency_key, request_summary=request_summary, parent_task_id=parent_task_id, delegation_id=delegation_id, child_sequence=child_sequence, depends_on_task_ids=depends_on_task_ids, deadline_at=deadline_at, conversation_id=conversation_id, trigger_message_id=trigger_message_id, trace_id=trace_id)
    except IntegrityError as exc:
        existing = db.query(Task).filter_by(tenant_id=tenant_id, owner_user_id=owner_user_id, idempotency_key=idempotency_key).one_or_none()
        if existing is not None and existing.request_summary == stored_summary and existing.agent_id == agent_id and existing.agent_version_id == agent_version_id:
            return existing
        raise ApiError("AI_TASK_DUPLICATE_IDEMPOTENCY", "idempotency key was used with different request", 409) from exc


def transition_task(
    db: Session,
    task: Task,
    target: TaskStatus,
    event_type: str,
    summary: str,
    trace_id: str | None = None,
    *,
    expected_state: TaskStatus | None = None,
) -> Task:
    current = TaskStatus(task.status)
    if expected_state is not None and current != expected_state:
        raise ApiError("AI_TASK_INVALID_TRANSITION", f"expected {expected_state}, got {current}", 409)
    if current in TERMINAL_STATES:
        raise ApiError("AI_TASK_ALREADY_TERMINAL", "task is already terminal", 409)
    if not can_transition(current, target):
        raise ApiError("AI_TASK_INVALID_TRANSITION", f"invalid transition {current} -> {target}", 409)
    next_event_seq = task.last_event_seq + 1
    # 并发失败只用 SAVEPOINT 回滚本函数自身的 UPDATE/事件追加：
    # 直接 db.rollback() 会连带回滚调用方已 flush 的审计与同批其他任务的迁移（P2-16）。
    with db.begin_nested():
        changed = db.execute(
            update(Task)
            .where(Task.id == task.id, Task.status == current, Task.last_event_seq == task.last_event_seq)
            .values(
                status=target,
                started_at=_now() if target == TaskStatus.RUNNING else Task.started_at,
                finished_at=_now() if target in TERMINAL_STATES else Task.finished_at,
                last_event_seq=Task.last_event_seq + 1,
            )
        )
        if changed.rowcount != 1:
            raise ApiError("AI_TASK_INVALID_TRANSITION", "task changed concurrently", 409)
        task.status = target
        task.last_event_seq = next_event_seq
        if target == TaskStatus.RUNNING:
            task.started_at = _now()
        if target in TERMINAL_STATES:
            task.finished_at = _now()
        db.add(TaskEvent(id=new_uuid7(), tenant_id=task.tenant_id, task_id=task.id, seq=task.last_event_seq, event_type=event_type, from_state=current, to_state=target, summary=sanitize_summary(summary), trace_id=trace_id))
    # 观测事件经 observability.sink Port 外发（状态机是统一收口点，P2-14）
    emit_event(
        "task.transition",
        {
            "task_id": str(task.id),
            "tenant_id": str(task.tenant_id),
            "from_state": current,
            "to_state": target,
            "event_type": event_type,
            "trace_id": trace_id,
        },
    )
    return task


def append_task_event(
    db: Session,
    task: Task,
    event_type: str,
    summary: str,
    trace_id: str | None = None,
    payload_summary: dict | None = None,
) -> None:
    """细粒度过程事件（如 tool_call/tool_result）：不改变任务状态，仅递增 seq 追加事件。

    乐观锁递增失败（并发）时静默丢弃——过程事件允许丢失，不影响状态一致性。
    """
    current = TaskStatus(task.status)
    if current in TERMINAL_STATES:
        return
    next_event_seq = task.last_event_seq + 1
    changed = db.execute(
        update(Task)
        .where(Task.id == task.id, Task.last_event_seq == task.last_event_seq)
        .values(last_event_seq=Task.last_event_seq + 1)
    )
    if changed.rowcount != 1:
        return
    task.last_event_seq = next_event_seq
    db.add(TaskEvent(id=new_uuid7(), tenant_id=task.tenant_id, task_id=task.id, seq=task.last_event_seq, event_type=event_type, from_state=None, to_state=current, summary=sanitize_summary(summary), payload_summary=payload_summary, trace_id=trace_id))
