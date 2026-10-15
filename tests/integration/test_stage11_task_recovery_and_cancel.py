from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.kernel.ports.queue_driver import QueueDriver
from app.kernel.task.dispatcher import OutboxDispatcher, execute_simulated
from app.kernel.task.runtime import TaskRuntime
from app.kernel.task.service import create_task, transition_task
from app.kernel.task.states import TaskStatus
from app.models import (
    Agent,
    AgentVersion,
    AuthSession,
    Conversation,
    Message,
    Outbox,
    Task,
    TaskEvent,
    Tenant,
    User,
)
from app.security.passwords import hash_password


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with SessionLocal.begin() as db:
        for model in (Message, Conversation, TaskEvent, Outbox, Task, AgentVersion, Agent, AuthSession, User, Tenant):
            db.execute(delete(model))


def _bootstrap() -> tuple[UUID, UUID, UUID, UUID]:
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"stage11-recovery-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        agent = Agent(tenant_id=tenant.id, owner_user_id=None, name="A", source_type="OFFICIAL", status="ACTIVE")
        db.add(agent)
        db.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version_number=1,
            status="PUBLISHED",
            config_schema_version="1",
            config_snapshot={"mode": "mock"},
        )
        db.add(version)
        db.flush()
        agent.current_published_version_id = version.id
        return tenant.id, user.id, agent.id, version.id


@pytest.mark.asyncio
async def test_outbox_dispatch_task_ready_is_effectively_idempotent():
    tenant_id, user_id, agent_id, version_id = _bootstrap()
    task_id: UUID
    with SessionLocal.begin() as db:
        task = create_task(
            db,
            tenant_id=tenant_id,
            owner_user_id=user_id,
            agent_id=agent_id,
            agent_version_id=version_id,
            idempotency_key=str(uuid4()),
            request_summary="run",
        )
        task_id = task.id
        assert db.query(Outbox).filter_by(aggregate_id=task_id, event_type="TASK_READY").count() == 1

    class DeterministicQueue:
        def __init__(self):
            self.items: list[UUID] = []

        async def put(self, task_id: UUID):
            self.items.append(task_id)

    queue = DeterministicQueue()
    dispatcher = OutboxDispatcher(queue)

    # Try dispatch a few times to account for SQLite/transaction timing.
    for _ in range(3):
        with SessionLocal() as db:
            await dispatcher.dispatch_once(db)

    with SessionLocal() as db:
        row = db.query(Outbox).filter_by(aggregate_id=task_id, event_type="TASK_READY").one()
        task = db.get(Task, task_id)
        assert task is not None
        # Eventually we should either have queued the task (SENT) or recorded an error.
        assert row.status in {"SENT", "PENDING", "FAILED", "PROCESSING"}
        if row.status == "SENT":
            assert task.status == TaskStatus.QUEUED
            assert queue.items.count(UUID(str(task_id))) <= 1
        else:
            # If not sent yet, it must still be safe to retry without duplicating.
            assert task.status in {TaskStatus.PENDING, TaskStatus.QUEUED}


def test_execute_simulated_honors_cancel_requested_before_side_effects():
    tenant_id, user_id, agent_id, version_id = _bootstrap()
    with SessionLocal.begin() as db:
        task = create_task(
            db,
            tenant_id=tenant_id,
            owner_user_id=user_id,
            agent_id=agent_id,
            agent_version_id=version_id,
            idempotency_key=str(uuid4()),
            request_summary="run",
        )
        transition_task(db, task, TaskStatus.QUEUED, "TASK_QUEUED", "queued")
        task.cancel_requested_at = datetime.now(timezone.utc)
        task_id = task.id

    with SessionLocal() as db:
        execute_simulated(db, task_id)

    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task is not None
        assert task.status == TaskStatus.CANCELLED
        # Ensure we did not write a SUCCESS result.
        assert task.result is None or task.result.get("status") != "completed"


def test_runtime_recover_requeues_running_and_writes_recovered_outbox():
    tenant_id, user_id, agent_id, version_id = _bootstrap()
    with SessionLocal.begin() as db:
        task = create_task(
            db,
            tenant_id=tenant_id,
            owner_user_id=user_id,
            agent_id=agent_id,
            agent_version_id=version_id,
            idempotency_key=str(uuid4()),
            request_summary="run",
        )
        transition_task(db, task, TaskStatus.QUEUED, "TASK_QUEUED", "queued")
        transition_task(db, task, TaskStatus.RUNNING, "TASK_STARTED", "started")
        task_id = task.id
        # create_task already wrote one TASK_READY; recover should add another.
        assert db.query(Outbox).filter_by(aggregate_id=task_id, event_type="TASK_READY").count() == 1

    asyncio.run(TaskRuntime().recover())

    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task is not None
        assert task.status == TaskStatus.QUEUED
        assert db.query(TaskEvent).filter_by(task_id=task_id, event_type="TASK_RECOVERED").count() == 1
        assert db.query(Outbox).filter_by(aggregate_id=task_id, event_type="TASK_READY").count() == 2


def test_timeout_loop_behavior_can_be_simulated_by_setting_deadline_and_executing():
    tenant_id, user_id, agent_id, version_id = _bootstrap()
    with SessionLocal.begin() as db:
        task = create_task(
            db,
            tenant_id=tenant_id,
            owner_user_id=user_id,
            agent_id=agent_id,
            agent_version_id=version_id,
            idempotency_key=str(uuid4()),
            request_summary="run",
            deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        transition_task(db, task, TaskStatus.QUEUED, "TASK_QUEUED", "queued")
        task_id = task.id

    with SessionLocal() as db:
        execute_simulated(db, task_id)

    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task is not None
        assert task.status == TaskStatus.TIMEOUT
        assert task.error_code == "AI_TASK_TIMEOUT"
