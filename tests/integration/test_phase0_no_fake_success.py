"""Phase 0 §2.5 真实入口验收：移除假成功 + lease fencing。"""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update

from app.builtin_plugins.queue_driver import InMemoryQueueDriver
from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.kernel.security_context import SecurityContext
from app.kernel.skill_gateway import SkillGateway
from app.kernel.task.dispatcher import OutboxDispatcher
from app.kernel.task.runtime import TaskRuntime
from app.kernel.task.service import create_task, transition_task
from app.kernel.task.states import TaskStatus
from app.main import app
from app.models import (
    Agent,
    AgentVersion,
    AuditLog,
    AuthSession,
    Message,
    Outbox,
    Task,
    TaskEvent,
    Tenant,
    User,
)
from app.security.passwords import hash_password
from tests.support.db_cleanup import cleanup_session
from tests.support.fakes import MockExecutor


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with cleanup_session() as db:
        for model in (Message, TaskEvent, Outbox, AuditLog, Task, AgentVersion, Agent, AuthSession, User, Tenant):
            db.execute(delete(model))


def _bootstrap() -> tuple[UUID, UUID, UUID, UUID]:
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"phase0-{uuid4()}")
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
async def test_capabilities_does_not_expose_mock_executor():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/z-torque/capabilities")
    assert response.status_code == 200
    data = response.json()["data"]
    assert "MOCK" not in data["tool_executors"]
    assert "MOCK" not in str(data)
    implementation = (data["model"].get("implementation") or "").lower()
    assert "mock" not in implementation


def test_mock_executor_is_test_di_only():
    gateway = SkillGateway()
    gateway.register(MockExecutor())
    assert "MOCK" in gateway.registered_tool_types()


def test_mock_model_gateway_is_not_exported_from_production():
    import app.builtin_plugins as builtin_plugins
    import app.builtin_plugins.model_gateway as model_gateway_module

    assert not hasattr(builtin_plugins, "MockModelGateway")
    assert not hasattr(model_gateway_module, "MockModelGateway")


def test_mock_chat_helper_is_not_in_production_service():
    import app.services.chat_service as chat_service

    assert not hasattr(chat_service, "handle_mock_chat")
    assert "[MOCK]" not in Path(chat_service.__file__).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_task_without_model_gateway_fails_explicitly_without_mock_message():
    """真实入口：POST /tasks 建任务 → Outbox 派发 → worker 在无模型配置下执行。"""
    tenant_id, user_id, agent_id, version_id = _bootstrap()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/z-torque/auth/login", json={"username": _username(user_id), "password": "pass"})
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        created = await client.post(
            "/api/z-torque/tasks",
            headers=headers,
            json={
                "agent_id": str(agent_id),
                "agent_version_id": str(version_id),
                "idempotency_key": str(uuid4()),
                "request_summary": "no model configured",
            },
        )
    assert created.status_code == 200
    task_id = UUID(created.json()["data"]["id"])

    queue = InMemoryQueueDriver()
    with SessionLocal() as db:
        await OutboxDispatcher(queue).dispatch_once(db)
    runtime = TaskRuntime(queue=queue, session_factory=SessionLocal, model_gateway=None)
    result = await runtime._execute(task_id)

    assert result == {"task_id": str(task_id), "status": "FAILED", "error_code": "AI_EXECUTOR_UNAVAILABLE"}
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task is not None
        assert task.status == TaskStatus.FAILED
        assert task.error_code == "AI_EXECUTOR_UNAVAILABLE"
        assert task.result is None
        assert db.scalar(select(Message).where(Message.task_id == task_id)) is None
        assert db.scalar(select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.event_type == "TASK_EXECUTOR_UNAVAILABLE")) is not None


def _username(user_id: UUID) -> str:
    with SessionLocal() as db:
        return db.get(User, user_id).username


def test_stale_epoch_cannot_write_terminal_state():
    tenant_id, user_id, agent_id, version_id = _bootstrap()
    with SessionLocal.begin() as db:
        task = create_task(
            db,
            ctx=SecurityContext(tenant_id=tenant_id, user_id=user_id),
            agent_id=agent_id,
            agent_version_id=version_id,
            idempotency_key=str(uuid4()),
            request_summary="fencing",
        )
        transition_task(db, task, TaskStatus.QUEUED, "TASK_QUEUED", "queued")
        transition_task(db, task, TaskStatus.RUNNING, "TASK_STARTED", "started")
        stale_epoch = task.attempt_epoch
        task_id = task.id
    with SessionLocal.begin() as db:
        db.execute(
            update(Task).where(Task.id == task_id).values(attempt_epoch=stale_epoch + 1)
        )
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        db.refresh(task)
        with pytest.raises(ApiError) as error:
            transition_task(
                db, task, TaskStatus.FAILED, "TASK_FAILED", "stale owner",
                expected_epoch=stale_epoch,
            )
        assert error.value.code == "AI_TASK_FENCING_REJECT"
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task.status == TaskStatus.RUNNING
        assert task.attempt_epoch == stale_epoch + 1
        audit = db.scalar(
            select(AuditLog).where(AuditLog.resource_id == task_id, AuditLog.action == "TASK_FENCING_REJECT")
        )
        assert audit is not None
