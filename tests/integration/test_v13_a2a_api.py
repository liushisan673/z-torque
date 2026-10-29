"""v1.3 M3 A2A 入站 HTTP 用例（此前只有代码审查，无 API 级验证）。

刻意不 mock `identity.py`：走真实登录 JWT + 真实 RBAC 表，验证
1. 未装配/未开启 → 503（能力先于权限）；
2. 无 token / 伪造 token → 403 `AI_A2A_DENIED`，且不写无法归属的审计行；
3. 有身份但缺 `a2a.invoke` → 403 且落 `A2A_DENIED`（拒绝留痕，带 actor/trace）；
4. 有权限但目标技能不存在 → 404（映射为稳定错误码，不 500），落 `A2A_FAILED`；
5. `/a2a/agents` 只投影已发布版本，提示词等本地资产不外泄，且缺权限同样 403。
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.models  # noqa: F401  # 确保模型注册
from app.assembly import assemble
from app.builtin_plugins.a2a_gateway.protocol import A2A_VERSION
from app.builtin_plugins.modules import resolve_profile
from app.config import get_settings
from app.db.session import SessionLocal, engine
from app.kernel.registry.active import active_registry, bind_registry
from app.main import app
from app.models import (
    Agent,
    AgentVersion,
    AuditLog,
    Permission,
    Role,
    Tenant,
    User,
    role_permission,
    user_role,
)
from app.security.passwords import hash_password

_STATE_KEYS = (
    "a2a_gateway",
    "port_registry",
    "skill_gateway",
    "model_gateway",
    "mcp_stdio_registry",
    "audit_export_service",
    "audit_archive_service",
    "assembly_result",
    "assembly_plugins",
)


@pytest.fixture(scope="module")
def enterprise_app():
    settings = get_settings()
    previous_registry = active_registry()
    previous_state = {key: getattr(app.state, key, None) for key in _STATE_KEYS}
    result = assemble(settings, engine, profile=resolve_profile("enterprise"))
    app.state.assembly_result = result
    app.state.assembly_plugins = tuple(manifest.name for manifest in result.plugins)
    for object_key, object_value in result.objects.items():
        setattr(app.state, object_key, object_value)
    app.state.port_registry = result.registry
    app.state.skill_gateway = result.skill_gateway
    app.state.model_gateway = result.model_gateway
    app.state.mcp_stdio_registry = result.stdio_registry

    yield result

    result.shutdown()
    for key, value in previous_state.items():
        if value is None:
            if hasattr(app.state, key):
                delattr(app.state, key)
        else:
            setattr(app.state, key, value)
    bind_registry(previous_registry)


@pytest.fixture()
def gateway(enterprise_app, monkeypatch):
    """打开 A2A 开关（默认 A2A_ENABLED=false，避免测试污染全局配置）。"""
    instance = enterprise_app.objects["a2a_gateway"]
    monkeypatch.setattr(instance, "_config", replace(instance._config, enabled=True, local_discovery=True))
    return instance


@pytest.fixture()
def seeded(enterprise_app):
    tenant_id = uuid4()
    with SessionLocal.begin() as db:
        db.add(Tenant(id=tenant_id, name=f"a2a-{tenant_id}"))
        db.flush()
        caller = User(tenant_id=tenant_id, username=f"caller-{uuid4()}", password_hash=hash_password("pass"))
        outsider = User(tenant_id=tenant_id, username=f"outsider-{uuid4()}", password_hash=hash_password("pass"))
        db.add_all([caller, outsider])
        db.flush()

        role = Role(tenant_id=tenant_id, name="a2a-caller")
        db.add(role)
        db.flush()
        permission = Permission(tenant_id=tenant_id, name="a2a.invoke")
        db.add(permission)
        db.flush()
        db.execute(role_permission.insert().values(role_id=role.id, permission_id=permission.id))
        db.execute(user_role.insert().values(user_id=caller.id, role_id=role.id))

        agent = Agent(tenant_id=tenant_id, name="published-agent", description="d", status="ACTIVE", source_type="USER")
        db.add(agent)
        db.flush()
        version = AgentVersion(
            tenant_id=tenant_id,
            agent_id=agent.id,
            version_number=1,
            status="PUBLISHED",
            config_snapshot={"capabilities": ["chat"], "system_prompt": "TOP SECRET PROMPT", "temperature": 0.7},
        )
        db.add(version)
        db.flush()
        agent.current_published_version_id = version.id
        names = {"caller": caller.username, "outsider": outsider.username}

    return {"tenant_id": tenant_id, **names}


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _login(client: AsyncClient, username: str) -> str:
    response = await client.post("/api/z-torque/auth/login", json={"username": username, "password": "pass"})
    assert response.status_code == 200, response.text
    return response.json()["data"]["access_token"]


def _envelope(**params) -> dict:
    return {
        "a2a_version": A2A_VERSION,
        "id": f"req-{uuid4()}",
        "params": {"route": "local", "skill_version_id": str(uuid4()), "arguments": {}, **params},
        "depth": 0,
    }


def _audit(tenant_id, action) -> list[AuditLog]:
    with SessionLocal() as db:
        return db.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == action)).all()


@pytest.mark.asyncio
async def test_disabled_gateway_is_503(enterprise_app, seeded):
    """默认 A2A_ENABLED=false：未开启必须先 503，而不是先报鉴权错误。"""
    async with await _client() as client:
        response = await client.post("/api/z-torque/a2a/invoke", json=_envelope())

    assert response.status_code == 503
    assert response.json()["code"] == "AI_A2A_DISABLED"


@pytest.mark.asyncio
async def test_invoke_without_token_or_with_forged_token_is_403(gateway, seeded):
    async with await _client() as client:
        anonymous = await client.post("/api/z-torque/a2a/invoke", json=_envelope())
        forged = await client.post(
            "/api/z-torque/a2a/invoke", headers={"Authorization": "Bearer not-a-jwt"}, json=_envelope()
        )

    assert anonymous.status_code == 403
    assert anonymous.json()["code"] == "AI_A2A_DENIED"
    assert forged.status_code == 403
    assert forged.json()["code"] == "AI_A2A_DENIED"
    # 身份未确定时不得写入无法归属的审计行（tenant 未知 → 无法隔离）
    assert _audit(seeded["tenant_id"], "A2A_DENIED") == []


@pytest.mark.asyncio
async def test_invoke_without_a2a_invoke_permission_is_denied_and_audited(gateway, seeded):
    async with await _client() as client:
        token = await _login(client, seeded["outsider"])
        response = await client.post(
            "/api/z-torque/a2a/invoke", headers={"Authorization": f"Bearer {token}"}, json=_envelope()
        )

    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "AI_A2A_DENIED"
    # 对外统一口径：不区分"token 无效"与"缺少权限"，避免给探测者提供判定信令
    assert body["message"] == "A2A inbound authentication or authorization failed"

    rows = _audit(seeded["tenant_id"], "A2A_DENIED")
    assert len(rows) == 1, "越权入站必须留痕"
    assert rows[0].actor_user_id is not None
    assert "a2a.invoke" in rows[0].summary
    assert rows[0].detail["reason"] == "missing a2a.invoke"


@pytest.mark.asyncio
async def test_authorized_invoke_maps_missing_target_to_stable_error(gateway, seeded):
    async with await _client() as client:
        token = await _login(client, seeded["caller"])
        response = await client.post(
            "/api/z-torque/a2a/invoke", headers={"Authorization": f"Bearer {token}"}, json=_envelope()
        )

    assert response.status_code == 404, "目标技能不存在必须是稳定错误码，不能 500"
    assert response.json()["code"] in {"AI_SKILL_NOT_FOUND", "AI_A2A_ROUTE_FAILED"}

    rows = _audit(seeded["tenant_id"], "A2A_FAILED")
    assert len(rows) == 1, "已鉴权但失败的路由同样要留痕"
    assert rows[0].detail["error_code"] == response.json()["code"]


@pytest.mark.asyncio
async def test_discovery_projection_hides_prompt_and_requires_permission(gateway, seeded):
    async with await _client() as client:
        outsider = await _login(client, seeded["outsider"])
        denied = await client.get("/api/z-torque/a2a/agents", headers={"Authorization": f"Bearer {outsider}"})
        assert denied.status_code == 403
        assert denied.json()["code"] == "AI_A2A_DENIED"

        caller = await _login(client, seeded["caller"])
        response = await client.get("/api/z-torque/a2a/agents", headers={"Authorization": f"Bearer {caller}"})
        assert response.status_code == 200, response.text

    items = response.json()["data"]["items"]
    assert [item["name"] for item in items] == ["published-agent"]
    assert items[0]["capabilities"] == ["chat"]
    assert "TOP SECRET PROMPT" not in response.text, "提示词属于本地资产，发现接口不得外泄"
    assert "temperature" not in response.text
    assert set(items[0]) == {"agent_id", "agent_version_id", "name", "description", "version", "capabilities"}


@pytest.mark.asyncio
async def test_invoke_rejects_oversized_payload_before_auth(gateway, seeded, monkeypatch):
    """入站体积闸门：超限即拒，且不得先把整个 body 读进内存/解析。"""
    monkeypatch.setattr(gateway, "_config", replace(gateway._config, max_request_bytes=1024))
    async with await _client() as client:
        token = await _login(client, seeded["caller"])
        response = await client.post(
            "/api/z-torque/a2a/invoke",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            content=b"{" + b" " * 4096 + b"}",
        )

    assert response.status_code == 403
    assert response.json()["code"] == "AI_A2A_DENIED"
    assert "exceeds" in response.json()["message"]
    # 体积闸门在认证之前：身份未确定时不落无法归属的审计行（与无 token 路径同口径）
    assert _audit(seeded["tenant_id"], "A2A_DENIED") == []


@pytest.mark.asyncio
async def test_inbound_trace_header_is_adopted_when_local_request_id_missing(gateway, seeded):
    """跨跳链路：上游 X-A2A-Trace 必须落进本地上下文与审计 trace_id。"""
    async with await _client() as client:
        token = await _login(client, seeded["caller"])
        response = await client.post(
            "/api/z-torque/a2a/invoke",
            headers={"Authorization": f"Bearer {token}", "X-A2A-Trace": "trace-hop-42"},
            json=_envelope(),
        )

    assert response.status_code == 404, response.text
    rows = _audit(seeded["tenant_id"], "A2A_FAILED")
    assert len(rows) == 1
    assert rows[0].trace_id == "trace-hop-42"
