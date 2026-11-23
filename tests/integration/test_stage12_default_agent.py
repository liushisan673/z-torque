"""阶段 12 M12-4：默认智能体（is_default）。

- 设置默认智能体接口（同租户互斥：设置新默认时清掉旧的）；
- 归档智能体不允许设为默认，归档时清除默认标记；
- 企微桥接解析目标智能体的 fallback 优先级：is_default > is_default_parent > 最早创建。
"""

import uuid
from datetime import datetime, timezone
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


@pytest.mark.asyncio
async def test_wecom_fallback_prefers_default_agent(tenant_user, monkeypatch):
    """企微桥接未配置专用智能体时，fallback 优先取 is_default，其次 is_default_parent。"""
    from app.config import get_settings
    from app.services.wecom_chat_bridge import WeComChatBridge
    from app.services.wecom_events import WeComEvent

    settings = get_settings()
    monkeypatch.setattr(settings, "wecom_chat_agent_id", "")
    monkeypatch.setattr(settings, "wecom_agent_id_app", "")
    monkeypatch.setattr(settings, "wecom_agent_id_bot", "")

    with SessionLocal.begin() as db:
        tenant_id = tenant_user["tenant_id"]
        owner = db.get(User, uuid.UUID(tenant_user["user_id"]))
        plain = Agent(tenant_id=tenant_id, owner_user_id=owner.id, name="plain", source_type="USER", status="ACTIVE")
        parent = Agent(tenant_id=tenant_id, owner_user_id=owner.id, name="parent", source_type="USER", status="ACTIVE", is_default_parent=True)
        default = Agent(tenant_id=tenant_id, owner_user_id=owner.id, name="default", source_type="USER", status="ACTIVE", is_default=True)
        db.add_all([plain, parent, default])
        db.flush()
        for agent in (plain, parent, default):
            version = AgentVersion(tenant_id=tenant_id, agent_id=agent.id, version_number=1, status="PUBLISHED", config_snapshot={"system_prompt": "sp", "model": "gpt-x"})
            db.add(version)
            db.flush()
            version.published_at = datetime.now(timezone.utc)
            agent.current_published_version_id = version.id
        ids = {"plain": str(plain.id), "parent": str(parent.id), "default": str(default.id)}

    bridge = WeComChatBridge(settings)
    event = WeComEvent(
        event_id=f"evt-default-{uuid4().hex[:8]}",
        event_type="wecom.message.received",
        channel="wecom_app",
        external_user_id="ext-user",
        text="你好",
    )
    with SessionLocal.begin() as db:
        owner = db.get(User, uuid.UUID(tenant_user["user_id"]))
        resolved = bridge._resolve_agent(db, owner, event)
        assert resolved is not None
        assert str(resolved.id) == ids["default"]
