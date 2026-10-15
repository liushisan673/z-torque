from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.main import app
from app.models import Agent, AgentVersion, AuthSession, Conversation, Message, Outbox, Role, Task, TaskEvent, Tenant, User
from app.security.passwords import hash_password


@pytest.fixture(autouse=True)
def users():
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
        db.execute(delete(Role))
        db.execute(delete(Tenant))
        tenant = Tenant(name=f"api-{uuid4()}")
        db.add(tenant)
        db.flush()
        db.add(User(tenant_id=tenant.id, username="owner", password_hash=hash_password("pass")))
        db.add(User(tenant_id=tenant.id, username="other", password_hash=hash_password("pass")))
    yield


@pytest.mark.asyncio
async def test_task_api_owner_isolation_and_pagination():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_login = await client.post("/api/z-torque/auth/login", json={"username": "owner", "password": "pass"})
        other_login = await client.post("/api/z-torque/auth/login", json={"username": "other", "password": "pass"})
        owner_token = owner_login.json()["data"]["access_token"]
        other_token = other_login.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {owner_token}"}
        create = await client.post("/api/z-torque/tasks", headers=headers, json={"agent_id": str(uuid4()), "agent_version_id": str(uuid4()), "idempotency_key": "api-1", "request_summary": "test"})
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
