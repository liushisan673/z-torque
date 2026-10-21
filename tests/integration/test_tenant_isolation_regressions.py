"""租户隔离与兼容接口回归用例（P0-3 / P0-4 / P1-9）。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.kernel.agent.service import bootstrap_default_parent, set_default_parent
from app.main import app
from app.models import Agent, AgentVersion, AuthSession, Permission, Role, Tenant, User, role_permission, user_role
from app.security.passwords import hash_password


def _official_agent(db, tenant_id, name: str) -> Agent:
    agent = Agent(tenant_id=tenant_id, owner_user_id=None, name=name, source_type="OFFICIAL", status="ACTIVE")
    db.add(agent)
    db.flush()
    version = AgentVersion(tenant_id=tenant_id, agent_id=agent.id, version_number=1, status="PUBLISHED")
    db.add(version)
    db.flush()
    agent.current_published_version_id = version.id
    return agent


def test_default_parent_is_tenant_scoped():
    """每个租户都能有自己的默认父 Agent，且互不干扰（P0-3 索引 + P0-4 更新范围）。"""
    with SessionLocal.begin() as db:
        tenant_a = Tenant(name=f"dp-a-{uuid4()}")
        tenant_b = Tenant(name=f"dp-b-{uuid4()}")
        db.add_all([tenant_a, tenant_b])
        db.flush()
        first_a = bootstrap_default_parent(db, tenant_id=tenant_a.id, owner_user_id=None)
        first_b = bootstrap_default_parent(db, tenant_id=tenant_b.id, owner_user_id=None)
        db.flush()

        actor = User(tenant_id=tenant_a.id, username=f"dp-actor-{uuid4()}", password_hash=hash_password("pass"))
        db.add(actor)
        db.flush()
        replacement = _official_agent(db, tenant_a.id, "replacement")
        set_default_parent(db, tenant_id=tenant_a.id, user_id=actor.id, agent_id=replacement.id)
        db.flush()

        assert replacement.is_default_parent is True
        assert first_a.is_default_parent is False
        # 另一个租户的默认父 Agent 不受影响
        assert db.scalar(select(Agent).where(Agent.id == first_b.id)).is_default_parent is True


@pytest.fixture
def compat_admin():
    username = f"compat-admin-{uuid4()}"
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"compat-{uuid4()}")
        db.add(tenant)
        db.flush()
        admin_role = Role(tenant_id=tenant.id, name="admin")
        permission = Permission(tenant_id=tenant.id, name="admin.manage")
        db.add_all([admin_role, permission])
        db.flush()
        db.execute(role_permission.insert().values(role_id=admin_role.id, permission_id=permission.id))
        admin = User(tenant_id=tenant.id, username=username, password_hash=hash_password("pass"))
        db.add(admin)
        db.flush()
        db.execute(user_role.insert().values(user_id=admin.id, role_id=admin_role.id))
    yield {"username": username, "password": "pass"}


@pytest.mark.asyncio
async def test_users_roles_compat_endpoints_are_usable(compat_admin):
    """P1-9：/users、/roles、/permissions 兼容接口不再 500。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/z-torque/auth/login", json={"username": compat_admin["username"], "password": compat_admin["password"]})
        token = login.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        assert (await client.get("/api/z-torque/users", headers=headers)).status_code == 200
        assert (await client.get("/api/z-torque/roles", headers=headers)).status_code == 200
        assert (await client.get("/api/z-torque/permissions", headers=headers)).status_code == 200

        created = await client.post(
            "/api/z-torque/users",
            headers=headers,
            json={"username": "compat-created", "password": "pass-123", "display_name": "Compat"},
        )
        assert created.status_code == 200, created.text
        user_id = created.json()["data"]["id"]
        patched = await client.patch(f"/api/z-torque/users/{user_id}", headers=headers, json={"display_name": "Renamed"})
        assert patched.status_code == 200, patched.text
        assert patched.json()["data"]["updated"] is True
        assert (await client.delete(f"/api/z-torque/users/{user_id}", headers=headers)).status_code == 200
