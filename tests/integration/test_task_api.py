from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.main import app
from app.models import (
    Agent,
    AgentVersion,
    AuditLog,
    AuthSession,
    Conversation,
    Message,
    Outbox,
    Role,
    Task,
    TaskEvent,
    Tenant,
    User,
)
from app.security.passwords import hash_password
from tests.support.db_cleanup import cleanup_session


@pytest.fixture(autouse=True)
def users():
    with cleanup_session() as db:
        db.execute(delete(AuditLog))
        db.execute(delete(Message))
        db.execute(delete(Conversation))
        db.execute(delete(TaskEvent))
        db.execute(delete(Outbox))
        db.execute(delete(Task))
        db.execute(delete(AgentVersion))
        db.execute(delete(Agent))
        db.execute(delete(AuthSession))
        db.execute(delete(User))
        db.execute(delete(Role))
        db.execute(delete(Tenant))
        tenant = Tenant(name=f"api-{uuid4()}")
        db.add(tenant)
        db.flush()
        owner = User(tenant_id=tenant.id, username="owner", password_hash=hash_password("pass"))
        db.add(owner)
        db.add(User(tenant_id=tenant.id, username="other", password_hash=hash_password("pass")))
        db.flush()
        agent = Agent(tenant_id=tenant.id, owner_user_id=owner.id, name="api-agent")
        db.add(agent)
        db.flush()
        version = AgentVersion(tenant_id=tenant.id, agent_id=agent.id, version_number=1, status="PUBLISHED")
        db.add(version)
        db.flush()
        agent.current_published_version_id = version.id
        ids = {"agent_id": str(agent.id), "agent_version_id": str(version.id)}
    yield ids


@pytest.mark.asyncio
async def test_task_api_owner_isolation_and_pagination(users):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_login = await client.post("/api/z-torque/auth/login", json={"username": "owner", "password": "pass"})
        other_login = await client.post("/api/z-torque/auth/login", json={"username": "other", "password": "pass"})
        owner_token = owner_login.json()["data"]["access_token"]
        other_token = other_login.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {owner_token}"}
        create = await client.post(
            "/api/z-torque/tasks",
            headers=headers,
            json={**users, "idempotency_key": "api-1", "request_summary": "test"},
        )
        assert create.status_code == 200
        task_id = create.json()["data"]["id"]
        listing = await client.get("/api/z-torque/tasks?offset=0&limit=1", headers=headers)
        assert listing.json()["data"]["total"] == 1
        forbidden = await client.get(f"/api/z-torque/tasks/{task_id}", headers={"Authorization": f"Bearer {other_token}"})
        assert forbidden.status_code == 404
        events = await client.get(f"/api/z-torque/tasks/{task_id}/events", headers=headers)
        assert events.status_code == 200
        cancel = await client.post(f"/api/z-torque/tasks/{task_id}/cancel", headers=headers)
        assert cancel.json()["data"]["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_task_api_rejects_unknown_agent_snapshot(users):
    """P1-5 回归：不可见的 agent/version 必须被接口层拒绝，不能凭任意标识驱动任务。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/z-torque/auth/login", json={"username": "owner", "password": "pass"})
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        unknown_agent = await client.post(
            "/api/z-torque/tasks",
            headers=headers,
            json={"agent_id": str(uuid4()), "agent_version_id": str(uuid4()), "idempotency_key": "api-2", "request_summary": "x"},
        )
        assert unknown_agent.status_code == 404
        mismatched_version = await client.post(
            "/api/z-torque/tasks",
            headers=headers,
            json={**users, "agent_version_id": str(uuid4()), "idempotency_key": "api-3", "request_summary": "x"},
        )
        assert mismatched_version.status_code == 409

@pytest.mark.asyncio
async def test_task_response_exposes_direct_delegation_relationship(users):
    """任务响应返回真实 parent/child 字段，供 UI 精确呈现直接委派关系。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/z-torque/auth/login", json={"username": "owner", "password": "pass"})
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        root_response = await client.post(
            "/api/z-torque/tasks",
            headers=headers,
            json={**users, "idempotency_key": "delegation-root", "request_summary": "root task"},
        )
        assert root_response.status_code == 200
        root = root_response.json()["data"]
        assert root["agent_id"] == users["agent_id"]
        assert root["parent_task_id"] is None
        assert root["child_sequence"] is None

        child_response = await client.post(
            "/api/z-torque/tasks",
            headers=headers,
            json={
                **users,
                "idempotency_key": "delegation-child",
                "request_summary": "child task",
                "parent_task_id": root["id"],
                "delegation_id": str(uuid4()),
                "child_sequence": 1,
            },
        )
        assert child_response.status_code == 200
        child = child_response.json()["data"]
        assert child["parent_task_id"] == root["id"]
        assert child["root_task_id"] == root["id"]
        assert child["child_sequence"] == 1
        assert child["delegation_depth"] == 1

        child_detail = await client.get(f"/api/z-torque/tasks/{child['id']}", headers=headers)
        assert child_detail.status_code == 200
        assert child_detail.json()["data"]["parent_task_id"] == root["id"]
