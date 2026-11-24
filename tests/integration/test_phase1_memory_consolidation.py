import json
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.main import app
from app.models import AuditLog, LongMemory, LongMemoryVersion, Tenant, User
from app.security.passwords import hash_password
from tests.support.ddl import fk_off, fk_on


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with SessionLocal.begin() as db:
        fk_off(db)
        for model in (LongMemoryVersion, LongMemory, AuditLog, User, Tenant):
            db.execute(delete(model))
        fk_on(db)


def _bootstrap():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"dream-{uuid4()}")
        db.add(tenant); db.flush()
        user = User(tenant_id=tenant.id, username=f"dream-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user); db.flush()
        return tenant.id, user.id, user.username


@pytest.mark.asyncio
async def test_dream_consolidation_cursor_diff_and_audit():
    tenant_id, user_id, username = _bootstrap()
    store = app.state.port_registry.try_get("memory.long")
    with SessionLocal.begin() as db:
        store.remember_scoped(db, tenant_id=tenant_id, user_id=user_id, key="fact:a", value="用户喜欢简洁回答", scope="USER", source_ref="test:a")
        store.remember_scoped(db, tenant_id=tenant_id, user_id=user_id, key="fact:b", value="用户喜欢简洁回答", scope="USER", source_ref="test:b")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/z-torque/auth/login", json={"username": username, "password": "pass"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        response = await client.post("/api/z-torque/memory/consolidate", headers=headers, json={"scope": "USER"})
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["conflicts_removed"] == 1
        assert len(data["merged"]) == 1
        second = await client.post("/api/z-torque/memory/consolidate", headers=headers, json={"scope": "USER"})
        assert second.json()["data"]["conflicts_removed"] == 0

    with SessionLocal() as db:
        rows = db.scalars(select(LongMemory).where(LongMemory.tenant_id == tenant_id, LongMemory.key.like("fact:%"))).all()
        assert sorted(row.status for row in rows) == ["ACTIVE", "DELETED"]
        active = next(row for row in rows if row.status == "ACTIVE")
        envelope = json.loads(active.value)
        assert envelope["fact"] == "用户喜欢简洁回答"
        assert envelope["diff"]["count"] == 1
        assert db.scalar(select(AuditLog).where(AuditLog.action == "MEMORY_CONSOLIDATED")) is not None
        versions = db.scalars(select(LongMemoryVersion).where(LongMemoryVersion.memory_id == active.id)).all()
        assert len(versions) >= 1
