from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.builtin_plugins.queue_driver import InMemoryQueueDriver
from app.db.session import SessionLocal
from app.kernel.security_context import SecurityContext
from app.kernel.task.dispatcher import OutboxDispatcher
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


def setup_task():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"reliability-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        task = create_task(
            db,
            ctx=SecurityContext(tenant_id=tenant.id, user_id=user.id),
            agent_id=uuid4(),
            agent_version_id=uuid4(),
            idempotency_key=str(uuid4()),
            request_summary="run",
        )
        return task.id


@pytest.mark.asyncio
async def test_outbox_dispatch_marks_sent_and_queues_task():
    task_id = setup_task()
    queue = InMemoryQueueDriver()
    dispatcher = OutboxDispatcher(queue)
    with SessionLocal() as db:
        assert await dispatcher.dispatch_once(db)
        row = db.query(Outbox).filter_by(aggregate_id=task_id).one()
        assert row.status == "SENT"
        assert await queue.get() == task_id
        assert db.get(Task, task_id).status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_outbox_queue_failure_returns_to_pending():
    task_id = setup_task()

    class FailingQueue:
        async def put(self, _task_id):
            raise RuntimeError("queue unavailable")

    with SessionLocal() as db:
        assert await OutboxDispatcher(FailingQueue()).dispatch_once(db)
        row = db.query(Outbox).filter_by(aggregate_id=task_id).one()
        assert row.status == "PENDING"
        assert row.last_error == "queue unavailable"
        assert row.attempt_count == 1


def test_recovery_requeues_running_task_and_writes_outbox():
    task_id = setup_task()
    with SessionLocal.begin() as db:
        task = db.get(Task, task_id)
        transition_task(db, task, TaskStatus.QUEUED, "TASK_QUEUED", "queued")
        transition_task(db, task, TaskStatus.RUNNING, "TASK_STARTED", "started")
    import asyncio
    asyncio.run(TaskRuntime(queue=InMemoryQueueDriver(), session_factory=SessionLocal).recover())
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task.status == TaskStatus.QUEUED
        assert db.query(TaskEvent).filter_by(task_id=task_id, event_type="TASK_RECOVERED").count() == 1
        assert db.query(Outbox).filter_by(aggregate_id=task_id, event_type="TASK_READY").count() == 2


def test_expired_task_has_timeout_state():
    task_id = setup_task()
    with SessionLocal.begin() as db:
        task = db.get(Task, task_id)
        task.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        transition_task(db, task, TaskStatus.QUEUED, "TASK_QUEUED", "queued")
    with SessionLocal.begin() as db:
        task = db.get(Task, task_id)
        task.error_code = "AI_TASK_TIMEOUT"
        transition_task(db, task, TaskStatus.TIMEOUT, "TASK_TIMEOUT", "deadline exceeded")
    with SessionLocal() as db:
        assert db.get(Task, task_id).status == TaskStatus.TIMEOUT
