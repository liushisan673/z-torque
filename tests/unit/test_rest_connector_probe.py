"""REST 连接器「测试连接」真实探测的单元测试。

根因：原实现中非 DB / 非 MCP 分支（即 REST）只校验了 status == ENABLED，
不发起任何网络请求，直接返回 status=ready，导致「显示连接就绪」是假成功。

本文件锁定新行为：
  - 有 base_url → 必须真的发 HEAD/GET，并按 HTTP 状态码给 verdict
  - 无 base_url → 诚实返回 probed=False（而非假装成功）
  - 401/403 → auth_required（远端可达但凭证有问题）
  - 超时 / 连接失败 → ApiError，不静默成功
"""

from __future__ import annotations

import httpx
import pytest

from app.api.routes.connectors import (
    _build_input_schema_from_params,
    _is_private_address,
    _probe_rest,
    _rest_auth_headers,
    _safe_tool_name,
)
from app.core.errors import ApiError
from app.security.secret_store import SecretStore

# ---------- 鉴权头构造 ----------

def test_bearer_token_builds_authorization_header():
    ref = "secret://" + SecretStore().encrypt("tok-123")
    headers, used = _rest_auth_headers({"auth_mode": "Bearer Token"}, ref)
    assert headers["Authorization"] == "Bearer tok-123"
    assert used is True


def test_api_key_header_uses_configured_header_name():
    ref = "secret://" + SecretStore().encrypt("k-abc")
    headers, used = _rest_auth_headers({"auth_mode": "API Key Header", "auth_header": "X-API-Key"}, ref)
    assert headers["X-API-Key"] == "k-abc"
    assert "Authorization" not in headers
    assert used is True


def test_anonymous_mode_sends_no_credential():
    ref = "secret://" + SecretStore().encrypt("tok")
    headers, used = _rest_auth_headers({"auth_mode": "匿名（需管理员允许）"}, ref)
    assert headers == {}
    assert used is False


def test_missing_secret_ref_means_no_auth():
    headers, used = _rest_auth_headers({"auth_mode": "Bearer Token"}, None)
    assert headers == {}
    assert used is False


# ---------- 真实探测 ----------

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _transport(status: int, server: str = "nginx") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"server": server})
    return httpx.MockTransport(handler)


def _patch_client(monkeypatch, transport: httpx.MockTransport) -> None:
    """把 httpx.AsyncClient 替换为带 MockTransport 的客户端（保留生产代码传入的其它参数）。"""

    def factory(**kw):
        kw.pop("transport", None)
        return _REAL_ASYNC_CLIENT(transport=transport, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_probe_marks_success_on_2xx(monkeypatch):
    _patch_client(monkeypatch, _transport(200))
    result = await _probe_rest({"base_url": "https://api.example.com/v1"}, None)
    assert result["probed"] is True
    assert result["verdict"] == "ready"
    assert result["http_status"] == 200
    assert result["auth_used"] is False


@pytest.mark.asyncio
async def test_probe_flags_auth_required_on_401(monkeypatch):
    _patch_client(monkeypatch, _transport(401))
    ref = "secret://" + SecretStore().encrypt("bad-token")
    result = await _probe_rest({"base_url": "https://api.example.com", "auth_mode": "Bearer Token"}, ref)
    assert result["probed"] is True
    assert result["verdict"] == "auth_required"
    assert result["http_status"] == 401
    assert result["auth_used"] is True


# ---------- 内网地址绕过环境代理 ----------

def test_is_private_address_detects_rfc1918():
    assert _is_private_address("192.168.10.239") is True
    assert _is_private_address("10.0.0.5") is True
    assert _is_private_address("172.16.0.1") is True
    assert _is_private_address("127.0.0.1") is True
    assert _is_private_address("169.254.169.254") is True


def test_is_private_address_treats_public_and_domains_as_public():
    assert _is_private_address("8.130.167.4") is False
    assert _is_private_address("8.8.8.8") is False
    assert _is_private_address("1.1.1.1") is False
    assert _is_private_address("api.example.com") is False  # 域名照常走代理
    assert _is_private_address(None) is False


@pytest.mark.asyncio
async def test_private_address_bypasses_env_proxy(monkeypatch):
    """内网 IP 必须绕过 http_proxy：否则代理回的 502 会被误读成对方服务挂了。"""
    seen: dict = {}

    def factory(**kw):
        seen.update(kw)
        kw.pop("transport", None)
        kw.pop("proxy", None)
        kw.pop("trust_env", None)
        return _REAL_ASYNC_CLIENT(transport=_transport(200), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    await _probe_rest({"base_url": "http://192.168.10.239:8081"}, None)
    assert seen.get("proxy") is None, "内网地址不应走代理"
    assert seen.get("trust_env") is False, "内网地址应禁用环境代理"


@pytest.mark.asyncio
async def test_public_address_still_uses_env_proxy(monkeypatch):
    """公网域名不受影响，照常使用环境代理。"""
    seen: dict = {}

    def factory(**kw):
        seen.update(kw)
        kw.pop("transport", None)
        return _REAL_ASYNC_CLIENT(transport=_transport(200), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    await _probe_rest({"base_url": "https://api.example.com"}, None)
    assert "proxy" not in seen and "trust_env" not in seen


@pytest.mark.asyncio
async def test_timeout_message_states_proxy_bypass(monkeypatch):
    """直连超时要给出可操作提示，并说明是否绕过代理。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")
    _patch_client(monkeypatch, httpx.MockTransport(handler))
    with pytest.raises(ApiError) as exc:
        await _probe_rest({"base_url": "http://192.168.10.239:8081"}, None)
    assert exc.value.code == "AI_CONNECTOR_UNAVAILABLE"
    assert "绕过代理" in exc.value.message
    assert "路由可达" in exc.value.message


@pytest.mark.asyncio
async def test_proxy_returned_502_is_attributed_to_proxy(monkeypatch):
    """带 Proxy-Connection 头的 502 必须点名是代理，不能冤枉目标服务。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, headers={"Proxy-Connection": "close"})
    _patch_client(monkeypatch, httpx.MockTransport(handler))
    with pytest.raises(ApiError) as exc:
        await _probe_rest({"base_url": "https://api.example.com"}, None)
    assert "中间代理" in exc.value.message
    assert "http_proxy" in exc.value.message


@pytest.mark.asyncio
async def test_probe_returns_partial_when_base_url_missing():
    # 没有 base_url：不假装成功，返回 probed=False
    result = await _probe_rest({}, None)
    assert result["probed"] is False
    assert "reason" in result


@pytest.mark.asyncio
async def test_probe_rejects_non_http_scheme():
    with pytest.raises(ApiError) as exc:
        await _probe_rest({"base_url": "ftp://example.com"}, None)
    assert exc.value.code == "AI_PARAM_INVALID"


@pytest.mark.asyncio
async def test_probe_raises_on_5xx(monkeypatch):
    _patch_client(monkeypatch, _transport(500))
    with pytest.raises(ApiError) as exc:
        await _probe_rest({"base_url": "https://api.example.com"}, None)
    assert exc.value.code == "AI_CONNECTOR_UNAVAILABLE"
    assert "500" in exc.value.message


@pytest.mark.asyncio
async def test_probe_raises_on_timeout(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout")
    _patch_client(monkeypatch, httpx.MockTransport(handler))
    with pytest.raises(ApiError) as exc:
        await _probe_rest({"base_url": "https://api.example.com"}, None)
    assert exc.value.code == "AI_CONNECTOR_UNAVAILABLE"
    assert "超时" in exc.value.message


@pytest.mark.asyncio
async def test_probe_raises_on_connect_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    _patch_client(monkeypatch, httpx.MockTransport(handler))
    with pytest.raises(ApiError) as exc:
        await _probe_rest({"base_url": "https://api.example.com"}, None)
    assert exc.value.code == "AI_CONNECTOR_UNAVAILABLE"


@pytest.mark.asyncio
async def test_probe_falls_back_to_get_when_head_rejected(monkeypatch):
    """网关不允许 HEAD(405) 时应回退 GET，而不是判定失败。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405, headers={"server": "gw"})
        return httpx.Response(200, headers={"server": "gw"})

    _patch_client(monkeypatch, httpx.MockTransport(handler))
    result = await _probe_rest({"base_url": "https://api.example.com"}, None)
    assert result["verdict"] == "ready"
    assert result["http_status"] == 200
    assert result["method_used"] == "GET"
    assert calls == ["HEAD", "GET"]


@pytest.mark.asyncio
async def test_probe_treats_405_on_get_as_reachable(monkeypatch):
    """仅支持 POST 的接口（如 Bocha /v1/web-search）：GET 405 仍证明远端可达，不能报检查失败。"""
    _patch_client(monkeypatch, _transport(405))
    result = await _probe_rest({"base_url": "https://api.bocha.cn/v1/web-search"}, None)
    assert result["verdict"] == "method_not_allowed"
    assert result["http_status"] == 405
    assert result["method_used"] == "GET"


@pytest.mark.asyncio
async def test_probe_rejects_loopback_in_production(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "app_debug", False)
    # 测试意图为「生产默认拒绝回环」：需同时隔离本地 .env 的显式放行列表，保证跨环境稳定
    monkeypatch.setattr(get_settings(), "connector_allowed_targets", [])
    with pytest.raises(ApiError) as exc:
        await _probe_rest({"base_url": "http://127.0.0.1:8080"}, None)
    assert exc.value.code == "AI_CONNECTOR_FORBIDDEN_HOST"


# ---------- OpenAPI 导入辅助函数 ----------

def test_build_input_schema_from_openapi_params():
    props = _build_input_schema_from_params([
        {"name": "user_id", "in": "path", "required": True, "schema": {"type": "string"}},
        {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer"}},
        {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["a", "b"]}},
    ])
    assert props["type"] == "object"
    assert props["required"] == ["user_id"]
    assert props["properties"]["user_id"]["type"] == "string"
    assert props["properties"]["limit"]["type"] == "integer"
    assert props["properties"]["status"]["enum"] == ["a", "b"]


def test_build_input_schema_excludes_header_params():
    """header 参数一律凭据化：不进 schema（模型不可见），由工具 config.headers 在执行期提供。"""
    props = _build_input_schema_from_params([
        {"name": "CSRF-TOKEN", "in": "header", "required": True, "schema": {"type": "string"}},
        {"name": "X-AUTH-TOKEN", "in": "header", "required": True, "schema": {"type": "string"}},
        {"name": "limit", "in": "query", "schema": {"type": "integer"}},
    ])
    assert "CSRF-TOKEN" not in props["properties"]
    assert "X-AUTH-TOKEN" not in props["properties"]
    assert set(props["properties"].keys()) == {"limit"}
    assert "required" not in props


def test_safe_tool_name_sanitizes_and_dedupes():
    seen: set[str] = set()
    n1 = _safe_tool_name("get /users/{id}", "", seen)
    assert n1[0].isalpha() or n1[0] == "_"
    assert "{" not in n1 and "}" not in n1 and "/" not in n1
    n2 = _safe_tool_name("get /users/{id}", "", seen)  # 同名 → 加后缀
    assert n2 != n1
    assert _safe_tool_name("", "", set()) == "tool"


def test_safe_tool_name_applies_prefix_and_length_cap():
    seen: set[str] = set()
    name = _safe_tool_name("a" * 100, "crm_", seen)
    assert name.startswith("crm_")
    assert len(name) <= 64
