from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.kernel.security_context import SecurityContext
from app.kernel.skill_confirmation import pause_task_for_confirmation, resolve_confirmation
from app.kernel.task.dispatcher import execute_simulated
from app.kernel.task.service import create_task, transition_task
from app.kernel.task.states import TaskStatus
from app.models import Agent, AgentVersion, AuthSession, Conversation, Message, Outbox, Task, TaskCheckpoint, TaskEvent, Tenant, User
from app.security.passwords import hash_password
from app.core.errors import ApiError


def _ctx(tenant_id, user_id):
    return SecurityContext(tenant_id=tenant_id, user_id=user_id)


@pytest.fixture(autouse=True)
def cleanup_task_data():
    yield
    with SessionLocal.begin() as db:
        db.execute(delete(Message))
        db.execute(delete(Conversation))
        db.execute(delete(TaskEvent))
        db.execute(delete(Outbox))
        db.execute(delete(Task))
        db.execute(delete(AgentVersion))
        db.execute(delete(Agent))
        db.execute(delete(AuthSession))


def setup_user():
    with SessionLocal.begin() as db:
        db.execute(delete(Message))
        db.execute(delete(Conversation))
        db.execute(delete(TaskEvent))
        db.execute(delete(Outbox))
        db.execute(delete(Task))
        db.execute(delete(AgentVersion))
        db.execute(delete(Agent))
        db.execute(delete(AuthSession))
        db.execute(delete(User))
        db.execute(delete(Tenant))
        tenant = Tenant(name=f"task-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        return tenant.id, user.id


def test_task_creation_is_atomic_and_idempotent():
    tenant_id, user_id = setup_user()
    agent_id, version_id = uuid4(), uuid4()
    with SessionLocal.begin() as db:
        first = create_task(db, ctx=_ctx(tenant_id, user_id), agent_id=agent_id, agent_version_id=version_id, idempotency_key="same", request_summary="run")
        second = create_task(db, ctx=_ctx(tenant_id, user_id), agent_id=agent_id, agent_version_id=version_id, idempotency_key="same", request_summary="run")
        assert first.id == second.id
        assert db.query(TaskEvent).filter_by(task_id=first.id).count() == 1
        assert db.query(Outbox).filter_by(aggregate_id=first.id).count() == 1


def test_idempotency_conflict_is_rejected():
    tenant_id, user_id = setup_user()
    with SessionLocal.begin() as db:
        create_task(db, ctx=_ctx(tenant_id, user_id), agent_id=uuid4(), agent_version_id=uuid4(), idempotency_key="conflict", request_summary="first")
        with pytest.raises(ApiError) as error:
            create_task(db, ctx=_ctx(tenant_id, user_id), agent_id=uuid4(), agent_version_id=uuid4(), idempotency_key="conflict", request_summary="second")
        assert error.value.code == "AI_TASK_DUPLICATE_IDEMPOTENCY"


def test_terminal_state_is_immutable():
    tenant_id, user_id = setup_user()
    with SessionLocal.begin() as db:
        task = create_task(db, ctx=_ctx(tenant_id, user_id), agent_id=uuid4(), agent_version_id=uuid4(), idempotency_key="terminal", request_summary="run")
        transition_task(db, task, TaskStatus.CANCELLED, "TASK_CANCELLED", "cancelled")
        with pytest.raises(ApiError):
            transition_task(db, task, TaskStatus.RUNNING, "TASK_STARTED", "started")


@pytest.mark.asyncio
async def test_duplicate_execution_is_claimed_once():
    tenant_id, user_id = setup_user()
    with SessionLocal.begin() as db:
        task = create_task(db, ctx=_ctx(tenant_id, user_id), agent_id=uuid4(), agent_version_id=uuid4(), idempotency_key="execute", request_summary="run")
        transition_task(db, task, TaskStatus.QUEUED, "TASK_QUEUED", "queued")
        task_id = task.id
    with SessionLocal() as db:
        execute_simulated(db, task_id)
    with SessionLocal() as db:
        execute_simulated(db, task_id)
        task = db.get(Task, task_id)
        assert task.status == TaskStatus.SUCCESS
        assert task.attempt_count == 1
        assert db.query(TaskEvent).filter_by(task_id=task_id).count() == 4


def test_parent_depth_constraint():
    tenant_id, user_id = setup_user()
    with SessionLocal.begin() as db:
        root = create_task(db, ctx=_ctx(tenant_id, user_id), agent_id=uuid4(), agent_version_id=uuid4(), idempotency_key="root", request_summary="root")
        child = create_task(db, ctx=_ctx(tenant_id, user_id), agent_id=uuid4(), agent_version_id=uuid4(), idempotency_key="child", request_summary="child", parent_task_id=root.id)
        with pytest.raises(ApiError):
            create_task(db, ctx=_ctx(tenant_id, user_id), agent_id=uuid4(), agent_version_id=uuid4(), idempotency_key="grandchild", request_summary="grandchild", parent_task_id=child.id)


def test_expired_deadline_is_timeout():
    tenant_id, user_id = setup_user()
    with SessionLocal.begin() as db:
        task = create_task(db, ctx=_ctx(tenant_id, user_id), agent_id=uuid4(), agent_version_id=uuid4(), idempotency_key="expired", request_summary="run", deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        transition_task(db, task, TaskStatus.QUEUED, "TASK_QUEUED", "queued")
        task_id = task.id
    import asyncio
    with SessionLocal() as db:
        execute_simulated(db, task_id)
    with SessionLocal() as db:
        assert db.get(Task, task_id).status == TaskStatus.TIMEOUT


def test_waiting_human_past_deadline_can_timeout():
    """逾期的 WAITING_HUMAN 任务必须可迁移到 TIMEOUT：否则 _timeout_loop 每 0.5s
    选中它 → transition 抛 AI_TASK_INVALID_TRANSITION → 整批超时处理中断。"""
    tenant_id, user_id = setup_user()
    with SessionLocal.begin() as db:
        task = create_task(db, ctx=_ctx(tenant_id, user_id), agent_id=uuid4(), agent_version_id=uuid4(), idempotency_key="waiting-timeout", request_summary="run", deadline_at=datetime.now(timezone.utc) + timedelta(hours=1))
        transition_task(db, task, TaskStatus.QUEUED, "TASK_QUEUED", "queued")
        transition_task(db, task, TaskStatus.RUNNING, "TASK_STARTED", "started")
        transition_task(db, task, TaskStatus.WAITING_HUMAN, "TASK_WAITING_HUMAN", "waiting approval")
        transition_task(db, task, TaskStatus.TIMEOUT, "TASK_TIMEOUT", "task timed out")
        assert task.status == TaskStatus.TIMEOUT


def test_task_can_pause_for_approval_more_than_once():
    """同任务两轮人工审批：checkpoint.task_id 唯一，第二次挂起必须复用原行
    （修复前 INSERT 触发 IntegrityError，任务以 AI_TASK_FAILED 告终）。"""
    tenant_id, user_id = setup_user()
    with SessionLocal.begin() as db:
        task = create_task(db, ctx=_ctx(tenant_id, user_id), agent_id=uuid4(), agent_version_id=uuid4(), idempotency_key="pause-twice", request_summary="run")
        transition_task(db, task, TaskStatus.QUEUED, "TASK_QUEUED", "queued")
        transition_task(db, task, TaskStatus.RUNNING, "TASK_STARTED", "started")
        first = pause_task_for_confirmation(
            db, tenant_id=tenant_id, user_id=user_id, task_id=task.id, reason="write tool a",
            snapshot={}, execution_context={}, completed_steps=[], pending_steps=[],
            pending_call={"tool_version_id": "a"}, idempotency_key=f"{task.id}:a",
        )
    with SessionLocal.begin() as db:
        # 他人审批通过（请求者不能自批）→ 任务回队 → worker 认领恢复 RUNNING
        approver = User(tenant_id=tenant_id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
        db.add(approver)
        db.flush()
        resolve_confirmation(db, tenant_id=tenant_id, confirmation_id=first.id, user_id=approver.id, approved=True)
        task = db.get(Task, task.id)
        assert task.status == TaskStatus.QUEUED
        transition_task(db, task, TaskStatus.RUNNING, "TASK_STARTED", "started")
        second = pause_task_for_confirmation(
            db, tenant_id=tenant_id, user_id=user_id, task_id=task.id, reason="write tool b",
            snapshot={}, execution_context={}, completed_steps=[], pending_steps=[],
            pending_call={"tool_version_id": "b"}, idempotency_key=f"{task.id}:b",
        )
        assert second.id != first.id
        # 唯一 checkpoint 行被复用并整体更新为第二轮挂起状态
        checkpoints = db.scalars(select(TaskCheckpoint).where(TaskCheckpoint.task_id == task.id)).all()
        assert len(checkpoints) == 1
        checkpoint = checkpoints[0]
        assert checkpoint.confirmation_id == second.id
        assert checkpoint.pending_call == {"tool_version_id": "b"}
        assert checkpoint.status == "WAITING_APPROVAL"
        assert task.status == TaskStatus.WAITING_HUMAN
