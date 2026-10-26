from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.kernel.security_context import SecurityContext
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


def _ctx(tenant_id, user_id):
    return SecurityContext(tenant_id=tenant_id, user_id=user_id)


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with SessionLocal.begin() as db:
        for model in (Message, Conversation, TaskEvent, Outbox, Task, AgentVersion, Agent, AuthSession, User, Tenant):
            db.execute(delete(model))


def setup_user():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"concurrency-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        return tenant.id, user.id


def test_transition_conflict_is_rejected():
    tenant_id, user_id = setup_user()
    with SessionLocal.begin() as db:
        task = create_task(db, ctx=_ctx(tenant_id, user_id), agent_id=uuid4(), agent_version_id=uuid4(), idempotency_key=str(uuid4()), request_summary="run")
        task_id = task.id
    with SessionLocal() as first, SessionLocal() as second:
        first_task = first.get(Task, task_id)
        second_task = second.get(Task, task_id)
        transition_task(first, first_task, TaskStatus.QUEUED, "TASK_QUEUED", "queued")
        first.commit()
        with pytest.raises(Exception):
            transition_task(second, second_task, TaskStatus.QUEUED, "TASK_QUEUED", "queued")


def test_duplicate_child_delegation_is_rejected():
    tenant_id, user_id = setup_user()
    with SessionLocal.begin() as db:
        root = create_task(db, ctx=_ctx(tenant_id, user_id), agent_id=uuid4(), agent_version_id=uuid4(), idempotency_key=str(uuid4()), request_summary="root")
        delegation_id = uuid4()
        create_task(db, ctx=_ctx(tenant_id, user_id), agent_id=uuid4(), agent_version_id=uuid4(), idempotency_key=str(uuid4()), request_summary="child", parent_task_id=root.id, delegation_id=delegation_id, child_sequence=1)
        with pytest.raises(Exception):
            create_task(db, ctx=_ctx(tenant_id, user_id), agent_id=uuid4(), agent_version_id=uuid4(), idempotency_key=str(uuid4()), request_summary="duplicate", parent_task_id=root.id, delegation_id=delegation_id, child_sequence=1)
