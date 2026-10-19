"""连接器 OpenAPI 批量导入 + REST 真实探测的端到端集成测试。

锁定两条关键行为：
  1. REST 连接器「测试连接」会真的发起网络请求，返回 http_status / auth_used，
     而不是只校验 ENABLED 就返回 ready（原实现是假成功）。
  2. OpenAPI 3.x 文档可一次性转成 N 个 HTTP_API 工具，URL 与 JSON 两种来源都支持。
"""

from __future__ import annotations

import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text

from app.db.session import SessionLocal
from app.main import app
from app.models import (
    Connector,
    ConnectorTool,
    Permission,
    Role,
    Tenant,
    User,
    role_permission,
    user_role,
)
from app.security.passwords import hash_password

CONNECTORS = "/api/z-torque/connectors"
# 真实 AsyncClient：monkeypatch 之后 httpx.AsyncClient 已变成 spy，必须先抓引用避免递归
_REAL_ASYNC_CLIENT = httpx.AsyncClient

# 一个最小可用的 OpenAPI 3.0 文档：2 个 path × 2 个 method = 4 个工具
SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "CRM API", "version": "1.0.0"},
    "servers": [{"url": "https://crm.example.com/api/v2"}],
    "paths": {
        "/users": {
            "get": {
                "operationId": "listUsers",
                "summary": "列出用户",
                "tags": ["users"],
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
            },
            "post": {
                "operationId": "createUser",
                "summary": "创建用户",
                "tags": ["users"],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
                        }
                    }
                },
            },
        },
        "/users/{user_id}": {
            "parameters": [{"name": "user_id", "in": "path", "required": True, "schema": {"type": "string"}}],
            "get": {"operationId": "getUser", "summary": "获取用户详情", "tags": ["users"]},
            "delete": {"operationId": "deleteUser", "summary": "删除用户", "tags": ["users"]},
        },
    },
}


@pytest.fixture(autouse=True)
def baseline():
    with SessionLocal.begin() as db:
        # 关闭 FK 校验：清理顺序不必精确匹配依赖图（与其它集成测试一致）
        db.execute(text("PRAGMA foreign_keys=OFF"))
        db.execute(delete(user_role))
        db.execute(delete(role_permission))
        db.execute(delete(Permission))
        db.execute(delete(ConnectorTool))
        db.execute(delete(Connector))
        db.execute(delete(User))
        db.execute(delete(Role))
        db.execute(delete(Tenant))
        db.execute(text("PRAGMA foreign_keys=ON"))

        tenant = Tenant(name="conn-tenant")
        db.add(tenant)
        db.flush()

        role = Role(tenant_id=tenant.id, name="admin")
        db.add(role)
        db.flush()

        perm_names = ["connector.read", "connector.manage"]
        perms = [Permission(tenant_id=tenant.id, name=n) for n in perm_names]
        db.add_all(perms)
        db.flush()

        admin = User(tenant_id=tenant.id, username="admin", password_hash=hash_password("pass"))
        db.add(admin)
        db.flush()

        for p in perms:
            db.execute(role_permission.insert().values(role_id=role.id, permission_id=p.id))
        db.execute(user_role.insert().values(user_id=admin.id, role_id=role.id))

        # 只读用户：只有 connector.read，用于验证导入需要 manage 权限
        ro_role = Role(tenant_id=tenant.id, name="readonly")
        db.add(ro_role)
        db.flush()
        ro_perm = db.scalar(select(Permission).where(Permission.name == "connector.read"))
        db.execute(role_permission.insert().values(role_id=ro_role.id, permission_id=ro_perm.id))
        ro_user = User(tenant_id=tenant.id, username="viewer", password_hash=hash_password("pass"))
        db.add(ro_user)
        db.flush()
        db.execute(user_role.insert().values(user_id=ro_user.id, role_id=ro_role.id))


async def _login(client: AsyncClient, username: str = "admin") -> str:
    resp = await client.post("/api/z-torque/auth/login", json={"username": username, "password": "pass"})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


async def _create_rest_connector(client: AsyncClient, token: str, name: str, base_url: str) -> str:
    resp = await client.post(
        CONNECTORS,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": name,
            "connector_type": "REST",
            "config": {"base_url": base_url, "auth_mode": "Bearer Token"},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


# ---------- OpenAPI 导入 ----------

@pytest.mark.asyncio
async def test_import_openapi_from_json_registers_all_operations():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        cid = await _create_rest_connector(client, token, "CRM", "https://crm.example.com/api/v2")

        resp = await client.post(
            f"{CONNECTORS}/{cid}/tools/import-openapi",
            headers={"Authorization": f"Bearer {token}"},
            json={"source": "json", "spec": SPEC},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        # 2 paths × (GET/POST + GET/DELETE) = 4
        assert len(data["registered"]) == 4, data
        methods = sorted(r["method"] for r in data["registered"])
        assert methods == ["DELETE", "GET", "GET", "POST"]
        # 每条工具都应带上完整 URL（servers[0].url + path）
        urls = {r["path"] for r in data["registered"]}
        assert urls == {"/users", "/users/{user_id}"}

        # 注册后 GET /tools 应能看到这 4 个工具
        listed = await client.get(f"{CONNECTORS}/{cid}/tools", headers={"Authorization": f"Bearer {token}"})
        assert listed.status_code == 200
        assert len(listed.json()["data"]) == 4

        # 工具名称应被清洗为合法标识符
        names = [t["name"] for t in listed.json()["data"]]
        assert all(n and n[0].isalpha() for n in names), names


@pytest.mark.asyncio
async def test_import_openapi_respects_method_filter_and_prefix():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        cid = await _create_rest_connector(client, token, "CRM2", "https://crm.example.com/api/v2")

        resp = await client.post(
            f"{CONNECTORS}/{cid}/tools/import-openapi",
            headers={"Authorization": f"Bearer {token}"},
            json={"source": "json", "spec": SPEC, "only_methods": ["GET"], "name_prefix": "crm_"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert len(data["registered"]) == 2
        assert all(r["method"] == "GET" for r in data["registered"])
        assert all(r["name"].startswith("crm_") for r in data["registered"])


@pytest.mark.asyncio
async def test_import_openapi_rejects_non_rest_connector():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        resp = await client.post(
            CONNECTORS,
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "DB-conn", "connector_type": "DB", "config": {"driver": "sqlite", "database": ":memory:"}},
        )
        assert resp.status_code == 200, resp.text
        cid = resp.json()["data"]["id"]

        resp = await client.post(
            f"{CONNECTORS}/{cid}/tools/import-openapi",
            headers={"Authorization": f"Bearer {token}"},
            json={"source": "json", "spec": SPEC},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "AI_PARAM_INVALID"


@pytest.mark.asyncio
async def test_import_openapi_rejects_spec_without_paths():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        cid = await _create_rest_connector(client, token, "CRM3", "https://crm.example.com")
        resp = await client.post(
            f"{CONNECTORS}/{cid}/tools/import-openapi",
            headers={"Authorization": f"Bearer {token}"},
            json={"source": "json", "spec": {"openapi": "3.0.0"}},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "AI_PARAM_INVALID"


@pytest.mark.asyncio
async def test_import_openapi_requires_manage_permission():
    """只有 connector.read 的只读用户不能导入工具（返回 403）。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin_token = await _login(client, "admin")
        viewer_token = await _login(client, "viewer")
        cid = await _create_rest_connector(client, admin_token, "CRM4", "https://crm.example.com")

        resp = await client.post(
            f"{CONNECTORS}/{cid}/tools/import-openapi",
            headers={"Authorization": f"Bearer {viewer_token}"},
            json={"source": "json", "spec": SPEC},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "AI_FORBIDDEN"


# ---------- REST 测试连接（真实探测） ----------

@pytest.mark.asyncio
async def test_rest_test_without_base_url_is_honestly_partial():
    """没配 base_url 时不应假装成功：partial=True + warning。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        resp = await client.post(
            CONNECTORS,
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "NoURL", "connector_type": "REST", "config": {"auth_mode": "Bearer Token"}},
        )
        assert resp.status_code == 200, resp.text
        cid = resp.json()["data"]["id"]

        test = await client.post(f"{CONNECTORS}/{cid}/test", headers={"Authorization": f"Bearer {token}"})
        assert test.status_code == 200, test.text
        data = test.json()["data"]
        assert data["status"] == "ready"
        assert data["partial"] is True
        assert "warning" in data
        # 关键：没有 http_status 就说明没有真的发出请求
        assert "http_status" not in data


@pytest.mark.asyncio
async def test_rest_test_really_calls_the_endpoint(monkeypatch):
    """配了 base_url 就必须真的发请求；用 MockTransport 断言请求确实被发出。"""
    import httpx as _httpx
    captured: list[str] = []

    class _SpyClient:
        def __init__(self, **kw):
            kw.pop("transport", None)
            self._real = _REAL_ASYNC_CLIENT(transport=_httpx.MockTransport(self._handler), **kw)

        async def _handler(self, request: _httpx.Request) -> _httpx.Response:
            captured.append(f"{request.method} {request.url}")
            assert request.headers.get("Authorization") == "Bearer secret-token"
            return _httpx.Response(200, headers={"server": "crm-gateway"})

        async def __aenter__(self):
            return self._real

        async def __aexit__(self, *a):
            await self._real.aclose()

    # 探测函数内部是 `import httpx` 后取 httpx.AsyncClient，因此直接给 httpx 模块打桩
    monkeypatch.setattr(_httpx, "AsyncClient", _SpyClient)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        # 先把明文 token 加密成 secret_ref，走真实 SecretStore
        from app.security.secret_store import SecretStore
        ref = "secret://" + SecretStore().encrypt("secret-token")
        resp = await client.post(
            CONNECTORS,
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "CRM-probe", "connector_type": "REST",
                  "config": {"base_url": "https://crm.example.com/api/v2", "auth_mode": "Bearer Token"},
                  "secret_ref": ref},
        )
        assert resp.status_code == 200, resp.text
        cid = resp.json()["data"]["id"]

        test = await client.post(f"{CONNECTORS}/{cid}/test", headers={"Authorization": f"Bearer {token}"})
        assert test.status_code == 200, test.text
        data = test.json()["data"]

    # 断言：真的发了请求 + 带上了凭证
    assert captured, "没有发起任何 HTTP 请求 → 又是假成功"
    assert captured[0].startswith("HEAD https://crm.example.com")
    assert data["status"] == "ready"
    assert data["http_status"] == 200
    assert data["auth_used"] is True
    assert data["server"] == "crm-gateway"
    assert data["endpoint"] == "https://crm.example.com/api/v2"
    assert data["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_rest_test_reports_auth_required_on_401(monkeypatch):
    """401 应报 auth_required（远端可达但凭证有问题），而不是假成功。"""
    import httpx as _httpx

    class _Client401:
        def __init__(self, **kw):
            kw.pop("transport", None)
            self._real = _REAL_ASYNC_CLIENT(
                transport=_httpx.MockTransport(lambda req: _httpx.Response(401, headers={"server": "gw"})), **kw
            )

        async def __aenter__(self):
            return self._real

        async def __aexit__(self, *a):
            await self._real.aclose()

    monkeypatch.setattr(_httpx, "AsyncClient", _Client401)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        cid = await _create_rest_connector(client, token, "CRM401", "https://crm.example.com")
        test = await client.post(f"{CONNECTORS}/{cid}/test", headers={"Authorization": f"Bearer {token}"})
        assert test.status_code == 200, test.text
        data = test.json()["data"]
        assert data["status"] == "auth_required"
        assert data["http_status"] == 401
        assert data["partial"] is True
        assert "鉴权" in data["warning"]


@pytest.mark.asyncio
async def test_rest_test_returns_error_on_unreachable_host(monkeypatch):
    """连接不上要返回错误，不能显示"连接就绪"。"""
    import httpx as _httpx

    class _ClientDown:
        def __init__(self, **kw):
            kw.pop("transport", None)

            def _raise(req):
                raise _httpx.ConnectError("connection refused")

            self._real = _REAL_ASYNC_CLIENT(transport=_httpx.MockTransport(_raise), **kw)

        async def __aenter__(self):
            return self._real

        async def __aexit__(self, *a):
            await self._real.aclose()

    monkeypatch.setattr(_httpx, "AsyncClient", _ClientDown)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        cid = await _create_rest_connector(client, token, "CRM-down", "https://crm.example.com")
        test = await client.post(f"{CONNECTORS}/{cid}/test", headers={"Authorization": f"Bearer {token}"})
        assert test.status_code == 502
        assert test.json()["code"] == "AI_CONNECTOR_UNAVAILABLE"
