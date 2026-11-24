"""Phase 1 §三-1：MCP 执行链路接线真实入口验收。

负向先行：生产不得注册恒失败 MCP 占位执行器；未连接才允许 AI_MCP_UNAVAILABLE。
正向用真实 SkillGateway.execute_async() 证明 MCP function tool 会调用 connector.call_tool。
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.kernel.security_context import SecurityContext
from app.kernel.skill_gateway import SkillGateway
from app.main import app
from app.models import (
    Connector,
    Permission,
    Role,
    Skill,
    SkillExecutionLog,
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
from tests.support.ddl import fk_off, fk_on

CONNECTORS = "/api/z-torque/connectors"


@pytest.fixture(autouse=True)
def _cleanup_phase1_mcp():
    with SessionLocal.begin() as db:
        fk_off(db)
        for model in (SkillExecutionLog, SkillVersionTool, ToolVersion, SkillVersion, Tool, Connector, user_role, role_permission, Permission, Role, User, Tenant):
            db.execute(delete(model))
        fk_on(db)
    yield
    with SessionLocal.begin() as db:
        fk_off(db)
        for model in (SkillExecutionLog, SkillVersionTool, ToolVersion, SkillVersion, Tool, Connector, user_role, role_permission, Permission, Role, User, Tenant):
            db.execute(delete(model))
        fk_on(db)


def _bootstrap():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"phase1-mcp-{uuid4()}")
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
        user = User(tenant_id=tenant.id, username=f"mcp-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        db.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
        return tenant.id, user.id, user.username


async def _login(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post("/api/z-torque/auth/login", json={"username": username, "password": "pass"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


class _FakeHttpConnector:
    """传输层替身：只替代远端 MCP Server，网关与数据库全走真实路径。"""

    calls: list[tuple[str, dict]] = []

    def __init__(self, endpoint: str, **kwargs) -> None:
        self.endpoint = endpoint

    async def negotiate(self) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": f"called:{name}:{arguments.get('q', '')}"}]}

    async def list_tools(self) -> dict:
        return {"tools": []}

    async def close(self) -> None:
        return None


def _build_mcp_skill(tenant_id, user_id, endpoint: str):
    with SessionLocal.begin() as db:
        connector = Connector(
            tenant_id=tenant_id,
            created_by=user_id,
            name="mcp-server",
            connector_type="MCP",
            status="ENABLED",
            config={"transport": "http", "endpoint": endpoint, "timeout_seconds": 5},
        )
        db.add(connector)
        db.flush()
        tool = Tool(tenant_id=tenant_id, owner_user_id=user_id, name="search", description="mcp search", tool_type="MCP", source_type="CONNECTOR", status="ACTIVE")
        db.add(tool)
        db.flush()
        version = ToolVersion(
            tenant_id=tenant_id, tool_id=tool.id, version_number=1, status="PUBLISHED",
            config_snapshot={"transport": "http", "endpoint": endpoint, "tool_name": "search", "connector_id": str(connector.id)},
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            required_permissions=[], requires_confirmation=False, timeout_seconds=5,
            snapshot_hash="phase1-mcp",
        )
        db.add(version)
        db.flush()
        tool.current_published_version_id = version.id
        skill = Skill(tenant_id=tenant_id, owner_user_id=user_id, name="mcp-skill", description="", skill_type="STANDARD", source_type="USER", status="ACTIVE")
        db.add(skill)
        db.flush()
        skill_version = SkillVersion(
            tenant_id=tenant_id, skill_id=skill.id, version_number=1, status="PUBLISHED",
            config_snapshot={}, required_permissions=[], requires_confirmation=False,
            snapshot_hash="phase1-mcp-skill",
        )
        db.add(skill_version)
        db.flush()
        db.add(SkillVersionTool(tenant_id=tenant_id, skill_version_id=skill_version.id, tool_version_id=version.id, sort_order=0))
        skill.current_published_version_id = skill_version.id
        return skill_version.id, version.id


def test_production_gateway_does_not_register_mcp_placeholder():
    """负向：生产 Manifest 不得注册恒失败 MCP 占位执行器。"""
    from app.builtin_plugins.plugins import BUILTIN_PLUGINS, SkillExecutorPlugin

    assert SkillExecutorPlugin in BUILTIN_PLUGINS
    source = open(SkillExecutorPlugin.contribute.__code__.co_filename, encoding="utf-8").read()
    assert "McpExecutor()" not in source


def test_negotiated_mcp_executor_unavailable_before_negotiation():
    """AI_MCP_UNAVAILABLE 只允许出现在未协商（未连接）状态。"""

    class NeverConnected:
        async def call_tool(self, name, arguments):
            raise AssertionError("must not call tool before negotiation")

    from app.kernel.skill_gateway import NegotiatedMcpExecutor

    executor = NegotiatedMcpExecutor(NeverConnected())  # type: ignore[arg-type]
    result = asyncio.run(executor.execute_async({"tool_name": "search"}, {"q": "x"}))
    assert result.status == "FAILED"
    assert result.error_code == "AI_MCP_UNAVAILABLE"


@pytest.mark.asyncio
async def test_capabilities_reports_mcp_execution_wiring():
    """真实 API 入口：capabilities 上报 MCP 执行链路已接线（非占位）。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/z-torque/capabilities")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert "MCP" not in data["tool_executors"]
    mcp = data["mcp"]
    assert mcp["execution"] == "AVAILABLE"
    assert mcp["http_discovery"] is True
    assert mcp["stdio_discovery"] is False


@pytest.mark.asyncio
async def test_mcp_tool_executes_real_call_through_gateway(monkeypatch):
    """真实入口：已登记 MCP 工具经 SkillGateway 调用传输层并留下执行日志。"""
    tenant_id, user_id, _ = _bootstrap()
    skill_version_id, _tool_version_id = _build_mcp_skill(tenant_id, user_id, "http://mcp.invalid/mcp")
    _FakeHttpConnector.calls = []
    monkeypatch.setattr("app.builtin_plugins.plugins.make_http_connector", _FakeHttpConnector)

    from app.assembly import assemble
    from app.config import get_settings
    from app.db.session import engine

    result = assemble(get_settings(), engine)
    gateway: SkillGateway = result.skill_gateway

    with SessionLocal() as db:
        ctx = SecurityContext(tenant_id=tenant_id, user_id=user_id, trace_id="phase1-mcp")
        execute_result = await gateway.execute_async(
            db,
            ctx,
            skill_version_id=skill_version_id,
            tool_version_id=_tool_version_id,
            arguments={"q": "route"},
            task_id=None,
            timeout_seconds=5,
        )
        db.commit()

    assert execute_result.status == "SUCCEEDED", execute_result
    assert _FakeHttpConnector.calls == [("search", {"q": "route"})]
    assert "called:search:route" in str(execute_result.data)

    with SessionLocal() as db:
        log = db.scalar(select(SkillExecutionLog).where(SkillExecutionLog.skill_version_id == skill_version_id))
        assert log is not None
        assert log.status == "SUCCEEDED"
        assert log.tool_version_id == _tool_version_id
