"""阶段 12 M12-4：默认智能体（is_default）。

- 设置默认智能体接口（同租户互斥：设置新默认时清掉旧的）；
- 归档智能体不允许设为默认，归档时清除默认标记。
"""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AgentVersion,
    AgentVersionSkill,
    AuditLog,
    Skill,
    SkillVersion,
    Tool,
    ToolVersion,
    User,
)
from app.security.passwords import hash_password


@pytest.fixture(autouse=True)
def _clean():
    with SessionLocal.begin() as db:
        db.execute(delete(AuditLog))
        db.execute(delete(AgentVersionSkill))
        db.execute(delete(SkillVersion))
        db.execute(delete(ToolVersion))
        db.execute(delete(Tool))
        db.execute(delete(Skill))
        db.execute(delete(AgentVersion))
        db.execute(delete(Agent))
        db.execute(delete(User))
    yield


@pytest.fixture()
def tenant_user():
    from app.models import Tenant

    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"p12-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username="owner", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        ids = {"tenant_id": str(tenant.id), "user_id": str(user.id)}
    yield ids


async def _login(client: AsyncClient) -> dict[str, str]:
    login = await client.post("/api/z-torque/auth/login", json={"username": "owner", "password": "pass"})
    return {"Authorization": f"Bearer {login.json()['data']['access_token']}"}


async def _create_published_agent(client: AsyncClient, headers: dict[str, str], name: str) -> dict:
    resp = await client.post(
        "/api/z-torque/agents",
        headers=headers,
        json={"name": name, "publish": True, "config_snapshot": {"system_prompt": "sp", "model": "gpt-x"}},
    )
    assert resp.status_code == 200
    return resp.json()["data"]


@pytest.mark.asyncio
async def test_set_default_agent_is_tenant_exclusive(tenant_user):
    """设置默认互斥：新默认生效后旧默认自动清除；列表返回 is_default 标记。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client)
        agent_a = await _create_published_agent(client, headers, "agent-a")
        agent_b = await _create_published_agent(client, headers, "agent-b")

        resp = await client.post(f"/api/z-torque/agents/{agent_a['id']}/set-default", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["is_default"] is True

        listing = await client.get("/api/z-torque/agents", headers=headers)
        flags = {item["id"]: item["is_default"] for item in listing.json()["data"]}
        assert flags[agent_a["id"]] is True
        assert flags[agent_b["id"]] is False

        resp = await client.post(f"/api/z-torque/agents/{agent_b['id']}/set-default", headers=headers)
        assert resp.status_code == 200
        listing = await client.get("/api/z-torque/agents", headers=headers)
        flags = {item["id"]: item["is_default"] for item in listing.json()["data"]}
        assert flags[agent_a["id"]] is False
        assert flags[agent_b["id"]] is True


@pytest.mark.asyncio
async def test_set_default_agent_rejects_archived(tenant_user):
    """归档智能体不允许设为默认；归档同时清除已有默认标记。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client)
        agent = await _create_published_agent(client, headers, "agent-arch")
        assert (await client.post(f"/api/z-torque/agents/{agent['id']}/set-default", headers=headers)).status_code == 200

        archived = await client.post(f"/api/z-torque/agents/{agent['id']}/archive", headers=headers)
        assert archived.status_code == 200
        assert archived.json()["data"]["is_default"] is False

        resp = await client.post(f"/api/z-torque/agents/{agent['id']}/set-default", headers=headers)
        assert resp.status_code == 422
