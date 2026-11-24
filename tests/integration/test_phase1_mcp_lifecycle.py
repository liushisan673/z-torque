"""Phase 1 §三-2：MCP 生命周期真实入口验收。

覆盖：分页发现 + connect/close 归属、白名单单事务登记、独占 owner、
失败回滚、模型工具可见/热更新、capabilities 如实上报。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

import app.api.routes.connectors as connectors_module
from app.db.session import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AgentVersion,
    AgentVersionSkill,
    AuditLog,
    Connector,
    Permission,
    Role,
    Skill,
    SkillVersion,
    SkillVersionTool,
    Tenant,
    Tool,
    ToolVersion,
    User,
    role_permission,
    user_role,
)
from app.security.passwords import hash_password
from app.services.tool_mapping_builder import build_tool_mappings
from tests.support.ddl import fk_off, fk_on

CONNECTORS = "/api/z-torque/connectors"


class _PagedConnector:
    """只替代远端 MCP Server；发现/登记/审计/DB 全走真实路径。"""

    instances: list["_PagedConnector"] = []
    updated = False

    def __init__(self, endpoint: str, **kwargs) -> None:
        self.endpoint = endpoint
        self.cursors: list[str | None] = []
        self.negotiate_count = 0
        self.close_count = 0
        self.instances.append(self)

    def _tools(self) -> list[dict]:
        search_schema = {
            "type": "object",
            "properties": {"q": {"type": "string", "description": "changed" if self.updated else "original"}},
        }
        return [
            {"name": "echo", "description": "echo text", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
            {"name": "search", "description": "search text", "inputSchema": search_schema},
        ]

    async def negotiate(self) -> None:
        self.negotiate_count += 1

    async def call_tool(self, name: str, arguments: dict):
        return {"content": [{"type": "text", "text": name}]}

    async def list_tools(self, cursor: str | None = None) -> dict:
        self.cursors.append(cursor)
        if cursor == "p2":
            return {"tools": [{"name": "hidden", "description": "page2", "inputSchema": {"type": "object"}}], "nextCursor": "p3"}
        return {"tools": self._tools(), "nextCursor": "p2"}

    async def close(self) -> None:
        self.close_count += 1


def _fake_factory(endpoint: str, **kwargs):
    return _PagedConnector(endpoint, **kwargs)


@pytest.fixture(autouse=True)
def _cleanup():
    lifecycle = getattr(app.state, "mcp_lifecycle", None)
    if lifecycle is not None:
        lifecycle.aclose()
    _PagedConnector.instances = []
    _PagedConnector.updated = False
    yield
    if lifecycle is not None:
        lifecycle.aclose()
    with SessionLocal.begin() as db:
        fk_off(db)
        for model in (
            AuditLog, AgentVersionSkill, SkillVersionTool, ToolVersion, Connector, Tool,
            SkillVersion, Skill, AgentVersion, Agent, user_role, role_permission,
            Permission, Role, User, Tenant,
        ):
            db.execute(delete(model))
        fk_on(db)


def _bootstrap():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"mcp-lifecycle-{uuid4()}")
        db.add(tenant)
        db.flush()
        role = Role(tenant_id=tenant.id, name="admin")
        db.add(role)
        db.flush()
        perms = [
            Permission(tenant_id=tenant.id, name="connector.read"),
            Permission(tenant_id=tenant.id, name="connector.manage"),
            Permission(tenant_id=tenant.id, name="tool.execute"),
            Permission(tenant_id=tenant.id, name="skill.execute"),
        ]
        db.add_all(perms)
        db.flush()
        for perm in perms:
            db.execute(role_permission.insert().values(role_id=role.id, permission_id=perm.id))
        user = User(tenant_id=tenant.id, username=f"mcp-life-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        db.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
        connector = Connector(
            tenant_id=tenant.id, created_by=user.id, name="mcp", connector_type="MCP",
            status="ENABLED", config={"transport": "http", "endpoint": "https://mcp.invalid/mcp", "target_policy": {"allow_private_network": True}},
        )
        db.add(connector)
        db.flush()
        return tenant.id, user.id, user.username, connector.id


async def _login(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post("/api/z-torque/auth/login", json={"username": username, "password": "pass"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def _bind_agent_tool(db, tenant_id, user_id, tool_version_id):
    agent = Agent(tenant_id=tenant_id, owner_user_id=user_id, name="A", source_type="USER", status="ACTIVE")
    db.add(agent)
    db.flush()
    version = AgentVersion(tenant_id=tenant_id, agent_id=agent.id, version_number=1, status="PUBLISHED", config_schema_version="1", config_snapshot={})
    db.add(version)
    db.flush()
    skill = Skill(tenant_id=tenant_id, owner_user_id=user_id, name="S", description=None, skill_type="STANDARD", source_type="USER", status="ACTIVE", official=False)
    db.add(skill)
    db.flush()
    skill_version = SkillVersion(tenant_id=tenant_id, skill_id=skill.id, version_number=1, status="PUBLISHED", config_schema_version="1", config_snapshot={}, requires_confirmation=False)
    db.add(skill_version)
    db.flush()
    db.add(AgentVersionSkill(tenant_id=tenant_id, agent_version_id=version.id, skill_version_id=skill_version.id))
    db.add(SkillVersionTool(tenant_id=tenant_id, skill_version_id=skill_version.id, tool_version_id=tool_version_id, sort_order=0))
    db.flush()
    return version


@pytest.mark.asyncio
async def test_capabilities_reports_mcp_lifecycle():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/z-torque/capabilities")
    assert response.status_code == 200, response.text
    data = response.json()["data"]["mcp"]
    assert data["execution"] == "AVAILABLE"
    assert data["lifecycle"] == "AVAILABLE"
    assert data["page_discovery"] is True
    assert data["whitelist_registration"] is True
    assert data["transactional_registration"] is True


@pytest.mark.asyncio
async def test_paged_discovery_owns_connect_and_close(monkeypatch):
    tenant_id, _user_id, username, connector_id = _bootstrap()
    monkeypatch.setattr(app.state.skill_gateway, "http_connector_factory", _fake_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, username)
        response = await client.post(
            f"{CONNECTORS}/{connector_id}/discover",
            headers=headers,
            json={"max_pages": 2, "owner_id": "discovery-owner"},
        )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [tool["name"] for tool in data["tools"]] == ["echo", "search", "hidden"]
    assert data["next_cursor"] == "p3"
    assert data["pages"] == 2
    assert len(_PagedConnector.instances) == 1
    instance = _PagedConnector.instances[0]
    assert instance.cursors == [None, "p2"]
    assert instance.negotiate_count == 1
    assert instance.close_count == 1


@pytest.mark.asyncio
async def test_whitelist_registration_is_visible_to_model_and_hot_updatable(monkeypatch):
    tenant_id, user_id, username, connector_id = _bootstrap()
    monkeypatch.setattr(app.state.skill_gateway, "http_connector_factory", _fake_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, username)
        registered = await client.post(
            f"{CONNECTORS}/{connector_id}/tools/register-from-discovery",
            headers=headers,
            json={"allowed_names": ["search"], "max_pages": 2, "owner_id": "register-owner"},
        )
        assert registered.status_code == 200, registered.text
        data = registered.json()["data"]
        assert [item["name"] for item in data["registered"]] == ["search"]
        assert data["skipped"] == []
        assert _PagedConnector.instances[0].close_count == 1

        with SessionLocal() as db:
            names = db.scalars(select(Tool.name).where(Tool.tenant_id == tenant_id)).all()
            assert names == ["search"]
            tool = db.scalar(select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == "search"))
            version_id = tool.current_published_version_id
            agent_version = _bind_agent_tool(db, tenant_id, user_id, version_id)
            mappings, tools = build_tool_mappings(db, agent_version, tenant_id)
            assert "search" in mappings
            assert tools[0]["function"]["parameters"]["properties"]["q"]["description"] == "original"
            db.commit()

        _PagedConnector.updated = True
        synced = await client.post(f"{CONNECTORS}/{connector_id}/tools/sync", headers=headers, json={"names": ["search"]})
        assert synced.status_code == 200, synced.text
        assert synced.json()["data"]["updated"][0]["fields"] == ["input_schema"]
        assert synced.json()["data"]["updated"][0]["version_number"] == 2

        with SessionLocal() as db:
            versions = db.scalars(select(ToolVersion).order_by(ToolVersion.version_number)).all()
            assert [item.version_number for item in versions] == [1, 2]
            tool = db.get(Tool, versions[0].tool_id)
            assert tool.current_published_version_id == versions[1].id
            agent_version = _bind_agent_tool(db, tenant_id, user_id, tool.current_published_version_id)
            _mappings, tools = build_tool_mappings(db, agent_version, tenant_id)
            assert tools[0]["function"]["parameters"]["properties"]["q"]["description"] == "changed"
            db.commit()


@pytest.mark.asyncio
async def test_owner_conflict_is_rejected_at_real_api_entry():
    tenant_id, _user_id, username, connector_id = _bootstrap()
    lifecycle = app.state.mcp_lifecycle
    token = lifecycle.registry.acquire_owner(tenant_id, connector_id, "holder")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers = await _login(client, username)
            response = await client.post(
                f"{CONNECTORS}/{connector_id}/discover",
                headers=headers,
                json={"owner_id": "intruder"},
            )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "AI_MCP_OWNER_CONFLICT"
        with SessionLocal() as db:
            assert db.scalar(select(AuditLog).where(AuditLog.action == "MCP_OWNER_CONFLICT")) is not None
    finally:
        lifecycle.registry.release_owner(token)


@pytest.mark.asyncio
async def test_whitelist_registration_rolls_back_all_tools(monkeypatch):
    tenant_id, _user_id, username, connector_id = _bootstrap()
    monkeypatch.setattr(app.state.skill_gateway, "http_connector_factory", _fake_factory)
    original_hash = connectors_module._snapshot_hash
    calls = {"count": 0}

    def _fail_second_hash(snapshot):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("boom during second tool")
        return original_hash(snapshot)

    monkeypatch.setattr(connectors_module, "_snapshot_hash", _fail_second_hash)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, username)
        response = await client.post(
            f"{CONNECTORS}/{connector_id}/tools/register-from-discovery",
            headers=headers,
            json={"allowed_names": ["echo", "search"], "owner_id": "rollback-owner"},
        )
    assert response.status_code == 500, response.text
    assert response.json()["code"] == "AI_MCP_TOOL_REGISTER_FAILED"
    with SessionLocal() as db:
        assert db.scalars(select(Tool.name).where(Tool.tenant_id == tenant_id)).all() == []
        assert db.scalar(select(AuditLog).where(AuditLog.action == "MCP_TOOL_REGISTER_FAILED")) is not None
    assert _PagedConnector.instances[0].close_count == 1
