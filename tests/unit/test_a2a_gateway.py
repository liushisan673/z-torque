"""A2A 网关专项单测（v1.3 §6.2~§6.6）。

覆盖此前完全没有用例的边界：
1. 协议校验：非法信封 / 非法路由 / 缺字段一律 `AI_A2A_*`（不 500）；
2. 深度：`X-A2A-Depth` 严格解析、出站 depth+1 越限在出站前失败；
3. peer 错误映射：4xx→DENIED、5xx/连接失败/超限/非 JSON→PEER_UNAVAILABLE、超时→TIMEOUT、熔断；
4. 入站鉴权：裸头不信任、无权限拒绝并**留痕**（先落审计再抛错）；
5. 扇出：并发上限、单条失败不放大；
6. discovery 只读投影：只暴露白名单键、只含已发布版本、租户内、会话外可读。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.builtin_plugins.a2a_gateway import peers as peers_module
from app.builtin_plugins.a2a_gateway.config import A2AConfig, PeerConfig
from app.builtin_plugins.a2a_gateway.discovery import AgentDiscovery
from app.builtin_plugins.a2a_gateway.federated import FederatedPrincipalRegistry
from app.builtin_plugins.a2a_gateway.gateway import A2AGateway
from app.builtin_plugins.a2a_gateway.identity import InboundIdentity
from app.builtin_plugins.a2a_gateway.peers import (
    A2A_DIRECTION_OUTBOUND,
    A2A_OUTBOUND_EVENT,
    A2A_OUTBOUND_EVENT_FIELDS,
    RESULT_CIRCUIT_OPEN,
    RESULT_ERROR,
    RESULT_OK,
    RESULT_TIMEOUT,
    PeerRegistry,
)
from app.builtin_plugins.a2a_gateway.protocol import (
    A2A_DEPTH_HEADER,
    A2A_ROUTE_HEADER,
    A2A_TRACE_HEADER,
    A2A_VERSION,
    parse_depth,
    parse_request,
    response_from_result,
)
from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.kernel.plugin_guard import CIRCUIT_OPEN_CODE, PluginGuardRegistry
from app.kernel.security_context import SecurityContext
from app.models import Agent, AgentVersion, AuditLog, Tenant
from tests.support.fakes import FakeRegistry, FakeSdk, FakeSecretProvider

INVOKE_URL = "https://peer.test/api/z-torque/a2a/invoke"


# ---------------------------------------------------------------- 协议层


def test_parse_request_rejects_malformed_envelopes_without_500():
    cases = [
        ("not-an-object", "AI_A2A_DENIED"),
        ({"a2a_version": "2.0", "params": {}}, "AI_A2A_DENIED"),
        ({"params": "nope"}, "AI_A2A_DENIED"),
        ({"params": {"route": "nowhere", "skill_version_id": str(uuid4())}}, "AI_A2A_ROUTE_FAILED"),
        ({"params": {"route": "local"}}, "AI_A2A_ROUTE_FAILED"),
        ({"params": {"route": "local", "skill_version_id": "not-a-uuid"}}, "AI_A2A_ROUTE_FAILED"),
        ({"params": {"route": "remote", "peer": "beta"}}, "AI_A2A_ROUTE_FAILED"),
        ({"params": {"route": "local", "skill_version_id": str(uuid4()), "arguments": ["x"]}}, "AI_A2A_DENIED"),
        ({"params": {"route": "local", "skill_version_id": str(uuid4())}, "depth": "deep"}, "AI_A2A_DENIED"),
    ]
    for payload, expected in cases:
        with pytest.raises(ApiError) as excinfo:
            parse_request(payload)
        assert excinfo.value.code == expected, payload
        assert excinfo.value.status_code in (400, 403, 404), "协议问题不得落成 500"


def test_parse_request_normalizes_valid_envelope():
    skill_version_id = uuid4()
    request = parse_request(
        {
            "a2a_version": A2A_VERSION,
            "id": "req-1",
            "params": {"route": "LOCAL ", "skill_version_id": str(skill_version_id), "arguments": {"q": 1}},
            "depth": 1,
        }
    )

    assert (request.request_id, request.route, request.skill_version_id, request.depth) == ("req-1", "local", skill_version_id, 1)
    assert request.arguments == {"q": 1}
    assert request.outbound_payload() == {
        "a2a_version": A2A_VERSION,
        "id": "req-1",
        "method": "a2a.invoke",
        "params": {"route": "local", "arguments": {"q": 1}, "skill_version_id": str(skill_version_id)},
        "depth": 1,
    }


def test_parse_depth_is_strict():
    assert parse_depth({}, max_depth=2) == 0
    assert parse_depth({A2A_DEPTH_HEADER: "2"}, max_depth=2) == 2
    assert parse_depth({"x-a2a-depth": " 1 "}, max_depth=2) == 1

    for raw in ("abc", "-1", "1.5"):
        with pytest.raises(ApiError) as excinfo:
            parse_depth({A2A_DEPTH_HEADER: raw}, max_depth=2)
        assert excinfo.value.code == "AI_A2A_DENIED", raw

    with pytest.raises(ApiError) as excinfo:
        parse_depth({A2A_DEPTH_HEADER: "3"}, max_depth=2)
    assert excinfo.value.code == "AI_A2A_DEPTH_EXCEEDED"


def test_response_mapping_never_leaks_internal_failure_as_success():
    @dataclass
    class Result:
        status: str
        data: object = None
        error_code: str | None = None
        error_message: str | None = None

    ok = response_from_result(request_id="r", result=Result(status="SUCCEEDED", data={"x": 1}))
    assert ok["status"] == "SUCCEEDED" and ok["data"] == {"x": 1}

    denied = response_from_result(
        request_id="r", result=Result(status="FAILED", error_code="AI_PERMISSION_DENIED", error_message="nope")
    )
    assert denied["status"] == "DENIED"
    assert denied["error"] == {"code": "AI_PERMISSION_DENIED", "message": "nope"}

    failed = response_from_result(request_id="r", result=Result(status="FAILED"))
    assert failed["status"] == "FAILED"
    assert failed["error"]["code"] == "AI_A2A_ROUTE_FAILED", "缺错误码时必须给稳定兜底码，不能空 error"


# ---------------------------------------------------------------- 入站鉴权


class _IdentityProvider:
    def __init__(self, *, ctx=None, error: Exception | None = None) -> None:
        self._ctx = ctx
        self._error = error
        self.calls: list[str] = []

    def authenticate(self, db, token):
        self.calls.append(token)
        if self._error is not None:
            raise self._error
        return self._ctx


class _AuthzProvider:
    def __init__(self, *, allowed: bool) -> None:
        self._allowed = allowed
        self.calls: list[tuple] = []

    def has_all(self, db, user_id, permissions):
        self.calls.append((user_id, tuple(permissions)))
        return self._allowed


def _tenant() -> tuple:
    """租户 + 真实用户：A2A_INVOKED/A2A_DENIED 审计行带外键，替身 id 会让留痕静默失败。"""
    tenant_id = uuid4()
    with SessionLocal.begin() as db:
        db.add(Tenant(id=tenant_id, name=f"t-{tenant_id}"))
        db.flush()
        from app.models import User

        user = User(tenant_id=tenant_id, username=f"u-{uuid4()}", password_hash="x")
        db.add(user)
        db.flush()
        user_id = user.id
    return tenant_id, user_id


def _denial_rows(tenant_id) -> list[AuditLog]:
    with SessionLocal() as db:
        return db.scalars(
            select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == "A2A_DENIED")
        ).all()


def test_missing_token_is_denied_before_touching_providers():
    tenant_id, user_id = _tenant()
    identity = _IdentityProvider(ctx=SecurityContext(tenant_id=tenant_id, user_id=user_id))
    sdk = FakeSdk(registry=FakeRegistry({"identity.provider": identity, "authz.provider": _AuthzProvider(allowed=True)}))

    with pytest.raises(ApiError) as excinfo:
        InboundIdentity(sdk).authenticate(None)

    assert excinfo.value.code == "AI_A2A_DENIED"
    assert identity.calls == [], "缺 token 不应再调用身份 Provider"


def test_authentication_failure_message_does_not_leak_provider_details():
    tenant_id, user_id = _tenant()
    identity = _IdentityProvider(error=RuntimeError("signature mismatch at /srv/keys/jwt.pem"))
    sdk = FakeSdk(registry=FakeRegistry({"identity.provider": identity, "authz.provider": _AuthzProvider(allowed=True)}))

    with pytest.raises(ApiError) as excinfo:
        InboundIdentity(sdk).authenticate("forged-token")

    assert excinfo.value.code == "AI_A2A_DENIED"
    assert "jwt.pem" not in excinfo.value.message
    assert _denial_rows(tenant_id) == [], "无法确定身份时不写入无法归属的审计行"


def test_missing_a2a_invoke_permission_is_denied_and_audited():
    tenant_id, user_id = _tenant()
    identity = _IdentityProvider(ctx=SecurityContext(tenant_id=tenant_id, user_id=user_id, session_id=uuid4()))
    authz = _AuthzProvider(allowed=False)
    sdk = FakeSdk(registry=FakeRegistry({"identity.provider": identity, "authz.provider": authz}))

    with pytest.raises(ApiError) as excinfo:
        InboundIdentity(sdk).authenticate("good-token", trace_id="trace-9")

    assert excinfo.value.status_code == 403
    assert authz.calls == [(user_id, ("a2a.invoke",))]

    rows = _denial_rows(tenant_id)
    assert len(rows) == 1, "拒绝必须先留痕"
    assert rows[0].actor_user_id is not None
    assert "missing a2a.invoke" in rows[0].summary
    assert rows[0].detail["reason"] == "missing a2a.invoke"
    assert rows[0].trace_id == "trace-9"


def test_authenticate_returns_frozen_context_with_trace():
    tenant_id, user_id = _tenant()
    session_id = uuid4()
    identity = _IdentityProvider(
        ctx=SecurityContext(tenant_id=tenant_id, user_id=user_id, session_id=session_id, trace_id="inner")
    )
    sdk = FakeSdk(registry=FakeRegistry({"identity.provider": identity, "authz.provider": _AuthzProvider(allowed=True)}))

    principal = InboundIdentity(sdk).authenticate("good-token", trace_id="outer")

    assert principal.principal_type == "local_jwt"
    assert principal.ctx == SecurityContext(tenant_id=tenant_id, user_id=user_id, session_id=session_id, trace_id="outer")
    with pytest.raises(Exception):
        principal.ctx.user_id = uuid4()  # frozen dataclass：执行期身份不可漂移


def test_missing_provider_ports_fail_closed_with_503():
    with pytest.raises(ApiError) as excinfo:
        InboundIdentity(FakeSdk(registry=FakeRegistry({}))).authenticate("token")
    assert excinfo.value.code == "AI_CAPABILITY_UNAVAILABLE"
    assert excinfo.value.status_code == 503


# ---------------------------------------------------------------- peer 出站


class _Response:
    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content


class _Client:
    """httpx.AsyncClient.post 替身：固定响应或固定异常，并记录出站参数。"""

    def __init__(self, *, response=None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict] = []

    async def post(self, url, *, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": dict(headers or {}), "timeout": timeout})
        if self._error is not None:
            raise self._error
        return self._response


def _config(**overrides) -> A2AConfig:
    base = dict(
        enabled=True,
        local_discovery=True,
        max_depth=2,
        max_fanout=3,
        default_timeout_seconds=5.0,
        identity_propagation="user_id_header",
        max_response_bytes=1024,
        remote_peers=(PeerConfig(peer_id="beta", base_url="https://peer.test"),),
        allowed_peers=frozenset({"beta"}),
        circuit_failure_threshold=2,
        circuit_cooldown_seconds=30.0,
    )
    base.update(overrides)
    return A2AConfig(**base)


def _peer_registry(config, *, client, permissions=("db.query", "network.outbound")):
    sdk = FakeSdk(settings=SimpleNamespace(), registry=FakeRegistry({}), permissions=permissions)
    guard = PluginGuardRegistry(failure_threshold=2, cooldown_seconds=30.0)
    return PeerRegistry(sdk, config, guard=guard, client=client)


def test_peer_whitelist_is_closed_by_default():
    registry = _peer_registry(_config(), client=_Client(response=_Response(200, b"{}")))

    with pytest.raises(ApiError) as excinfo:
        registry.resolve("gamma")
    assert excinfo.value.code == "AI_A2A_PEER_UNAVAILABLE"
    assert excinfo.value.status_code == 503

    # 白名单放行但未配置 base_url → 404（"未授权"与"配置缺失"语义必须区分）
    registry2 = _peer_registry(_config(allowed_peers=frozenset({"beta", "orphan"})), client=_Client(response=_Response(200, b"{}")))
    with pytest.raises(ApiError) as excinfo2:
        registry2.resolve("orphan")
    assert excinfo2.value.code == "AI_A2A_ROUTE_FAILED"


@pytest.mark.asyncio
async def test_peer_invoke_requires_network_outbound_declaration():
    client = _Client(response=_Response(200, b"{}"))
    registry = _peer_registry(_config(), client=client, permissions=("db.query",))

    with pytest.raises(PermissionError):
        await registry.invoke(PeerConfig(peer_id="beta", base_url="https://peer.test"), {"id": "1"})

    assert client.calls == [], "未自证权限前不得发起任何出站请求"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error", "expected_code", "expected_status"),
    [
        (_Response(500, b"{}"), None, "AI_A2A_PEER_UNAVAILABLE", 503),
        (_Response(403, b"{}"), None, "AI_A2A_DENIED", 403),
        (_Response(200, b"not-json"), None, "AI_A2A_PEER_UNAVAILABLE", 503),
        (_Response(200, b"[1,2,3]"), None, "AI_A2A_PEER_UNAVAILABLE", 503),
        (_Response(200, b"x" * 2000), None, "AI_A2A_PEER_UNAVAILABLE", 503),
        (None, httpx.ConnectError("boom"), "AI_A2A_PEER_UNAVAILABLE", 503),
        (None, httpx.ReadTimeout("slow"), "AI_A2A_TIMEOUT", 504),
    ],
)
async def test_peer_error_mapping(response, error, expected_code, expected_status):
    registry = _peer_registry(_config(), client=_Client(response=response, error=error))

    with pytest.raises(ApiError) as excinfo:
        await registry.invoke(PeerConfig(peer_id="beta", base_url="https://peer.test"), {"id": "1"})

    assert excinfo.value.code == expected_code
    assert excinfo.value.status_code == expected_status


@pytest.mark.asyncio
async def test_peer_success_forwards_only_safe_headers():
    client = _Client(response=_Response(200, b'{"status":"SUCCEEDED","data":{"ok":true}}'))
    registry = _peer_registry(_config(), client=client)
    peer = PeerConfig(peer_id="beta", base_url="https://peer.test")

    body = await registry.invoke(
        peer,
        {"id": "1"},
        extra_headers={A2A_DEPTH_HEADER: "1", A2A_TRACE_HEADER: "t-1", "X-User-Id": "forged", "Authorization": "Bearer evil"},
    )

    assert body["status"] == "SUCCEEDED"
    sent = client.calls[0]
    assert sent["url"] == INVOKE_URL
    assert sent["headers"][A2A_DEPTH_HEADER] == "1"
    assert sent["headers"][A2A_TRACE_HEADER] == "t-1"
    assert "X-User-Id" not in sent["headers"], "信令头白名单必须拒绝调用方伪造身份"
    assert sent["headers"].get("Authorization") is None
    assert sent["timeout"] == 5.0


@pytest.mark.asyncio
async def test_peer_breaker_opens_after_threshold():
    client = _Client(response=_Response(500, b"{}"))
    registry = _peer_registry(_config(), client=client)
    peer = PeerConfig(peer_id="beta", base_url="https://peer.test")

    for _ in range(2):
        with pytest.raises(ApiError):
            await registry.invoke(peer, {"id": "1"})

    with pytest.raises(ApiError) as excinfo:
        await registry.invoke(peer, {"id": "1"})
    assert excinfo.value.code == CIRCUIT_OPEN_CODE
    assert len(client.calls) == 2, "熔断打开后必须快速失败，不得继续打到对端"


@pytest.mark.asyncio
async def test_peer_breaker_uses_plugin_config_not_global_default():
    """A2A_CIRCUIT_FAILURE_THRESHOLD 必须真的生效（全局 PLUGIN_BREAKER_* 只是兜底默认）。"""
    client = _Client(response=_Response(500, b"{}"))
    sdk = FakeSdk(settings=SimpleNamespace(), registry=FakeRegistry({}), permissions=("db.query", "network.outbound"))
    guard = PluginGuardRegistry(failure_threshold=10, cooldown_seconds=30.0)
    registry = PeerRegistry(sdk, _config(circuit_failure_threshold=1), guard=guard, client=client)
    peer = PeerConfig(peer_id="beta", base_url="https://peer.test")

    with pytest.raises(ApiError) as first:
        await registry.invoke(peer, {"id": "1"})
    assert first.value.code != CIRCUIT_OPEN_CODE

    with pytest.raises(ApiError) as second:
        await registry.invoke(peer, {"id": "1"})
    assert second.value.code == CIRCUIT_OPEN_CODE, "配置阈值 1 必须覆盖全局阈值 10"
    assert len(client.calls) == 1, "熔断打开后不得继续打到对端"


# ---------------------------------------------------------------- 网关编排


@dataclass
class _Result:
    status: str
    data: object = None
    error_code: str | None = None
    error_message: str | None = None


class _SkillGateway:
    def __init__(self, result: _Result | None = None, *, delay: float = 0.0) -> None:
        self._result = result or _Result(status="SUCCEEDED", data={"echo": True})
        self._delay = delay
        self.calls: list[dict] = []
        self.active = 0
        self.max_active = 0

    async def execute_async(self, db, *, ctx, skill_version_id, arguments, timeout_seconds):
        self.calls.append({"ctx": ctx, "skill_version_id": skill_version_id, "arguments": arguments})
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            return self._result
        finally:
            self.active -= 1


def _gateway(*, config=None, skill_gateway=None, permissions=("db.query", "network.outbound"), client=None, allowed=True):
    tenant_id, user_id = _tenant()
    ctx = SecurityContext(tenant_id=tenant_id, user_id=user_id, trace_id="t-1")
    ports = {
        "identity.provider": _IdentityProvider(ctx=ctx),
        "authz.provider": _AuthzProvider(allowed=allowed),
    }
    sdk = FakeSdk(settings=SimpleNamespace(), registry=FakeRegistry(ports), permissions=permissions)
    cfg = config or _config()
    peers = PeerRegistry(sdk, cfg, guard=PluginGuardRegistry(failure_threshold=5, cooldown_seconds=30.0), client=client)
    gateway = A2AGateway(
        sdk,
        cfg,
        identity=InboundIdentity(sdk),
        discovery=AgentDiscovery(sdk),
        peers=peers,
        skill_gateway=skill_gateway or _SkillGateway(),
    )
    return gateway, tenant_id, user_id


def _local_payload(**params):
    return {
        "a2a_version": A2A_VERSION,
        "id": "req-local",
        "params": {"route": "local", "skill_version_id": str(uuid4()), "arguments": {}, **params},
        "depth": 0,
    }


def _audit_actions(tenant_id) -> list[str]:
    with SessionLocal() as db:
        return [
            item.action
            for item in db.scalars(
                select(AuditLog).where(AuditLog.tenant_id == tenant_id).order_by(AuditLog.created_at.asc())
            ).all()
        ]


@pytest.mark.asyncio
async def test_disabled_gateway_fails_closed_before_authentication():
    gateway, tenant_id, _ = _gateway(config=_config(enabled=False))

    with pytest.raises(ApiError) as excinfo:
        await gateway.invoke(headers={"Authorization": "Bearer x"}, payload=_local_payload())

    assert excinfo.value.code == "AI_A2A_DISABLED"
    assert excinfo.value.status_code == 503
    assert _audit_actions(tenant_id) == [], "开关关闭不得产生调用审计噪声"


@pytest.mark.asyncio
async def test_local_invoke_delegates_and_audits_success():
    skill_gateway = _SkillGateway(_Result(status="SUCCEEDED", data={"echo": 1}))
    gateway, tenant_id, user_id = _gateway(skill_gateway=skill_gateway)

    response = await gateway.invoke(headers={"Authorization": "Bearer good"}, payload=_local_payload(arguments={"q": "x"}))

    assert response["status"] == "SUCCEEDED"
    assert response["data"] == {"echo": 1}
    assert response["a2a_version"] == A2A_VERSION
    assert skill_gateway.calls[0]["ctx"].user_id == user_id
    assert skill_gateway.calls[0]["arguments"] == {"q": "x"}
    assert _audit_actions(tenant_id) == ["A2A_INVOKED"]


@pytest.mark.asyncio
async def test_local_denial_maps_to_denied_and_is_audited():
    skill_gateway = _SkillGateway(
        _Result(status="FAILED", error_code="AI_PERMISSION_DENIED", error_message="no skill permission")
    )
    gateway, tenant_id, _ = _gateway(skill_gateway=skill_gateway)

    response = await gateway.invoke(headers={"Authorization": "Bearer good"}, payload=_local_payload())

    assert response["status"] == "DENIED"
    assert response["error"]["code"] == "AI_PERMISSION_DENIED"
    with SessionLocal() as db:
        rows = db.scalars(
            select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == "A2A_FAILED")
        ).all()
        assert len(rows) == 1, "技能层拒绝必须留痕（A2A_FAILED + error_code）"
        assert rows[0].detail["error_code"] == "AI_PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_remote_route_bumps_depth_and_rejects_beyond_limit():
    client = _Client(response=_Response(200, b'{"status":"SUCCEEDED","data":{"pong":true}}'))
    gateway, tenant_id, _ = _gateway(client=client, config=_config(max_depth=2))
    remote = {"a2a_version": A2A_VERSION, "id": "r", "params": {"route": "remote", "peer": "beta", "skill": "ping"}}

    ok = await gateway.invoke(headers={"Authorization": "Bearer good", A2A_DEPTH_HEADER: "1"}, payload=remote)

    assert ok["status"] == "SUCCEEDED"
    assert (ok["peer"], ok["depth"]) == ("beta", 2)
    sent = client.calls[0]
    assert sent["json"]["depth"] == 2, "出站 depth 必须 +1，避免对端无限回环"
    assert sent["headers"][A2A_ROUTE_HEADER] == "outbound"
    assert sent["headers"]["X-User-Id"], "identity_propagation=user_id_header 时应携带调用方身份"

    with pytest.raises(ApiError) as excinfo:
        await gateway.invoke(headers={"Authorization": "Bearer good", A2A_DEPTH_HEADER: "2"}, payload=remote)
    assert excinfo.value.code == "AI_A2A_DEPTH_EXCEEDED"
    assert len(client.calls) == 1, "越限必须在出站前失败"

    with SessionLocal() as db:
        denied = db.scalars(
            select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == "A2A_DENIED")
        ).all()
        assert len(denied) == 1
        assert denied[0].detail["depth"] == 2


@pytest.mark.asyncio
async def test_remote_with_pinned_skill_version_is_executed_by_peer_not_rerouted():
    """钉死了 skill_version_id 时，出站载荷必须改写成 `route=local`（请对端执行）。

    真联调抓到的缺陷：原样转发 `params.peer`（发送方命名空间里的名字）会让对端拿它去
    查自己的 peer 表 → 503 或打错人。载荷语义必须是"对端要做什么"，不是"我是怎么路由的"。
    """
    client = _Client(response=_Response(200, b'{"status":"SUCCEEDED","data":{"ok":true}}'))
    gateway, _, _ = _gateway(client=client, config=_config(max_depth=2))
    skill_version_id = uuid4()

    await gateway.invoke(
        headers={"Authorization": "Bearer good"},
        payload={
            "a2a_version": A2A_VERSION,
            "id": "r",
            "params": {"route": "remote", "peer": "beta", "skill": "ping", "skill_version_id": str(skill_version_id)},
        },
    )

    sent = client.calls[0]["json"]
    assert sent["params"]["route"] == "local", "调用方已钉死版本，对端应执行而不是再路由"
    assert sent["params"]["skill_version_id"] == str(skill_version_id)
    assert sent["depth"] == 1


@pytest.mark.asyncio
async def test_full_context_token_propagates_trusted_token_not_forged_header():
    client = _Client(response=_Response(200, b'{"status":"SUCCEEDED"}'))
    gateway, _, _ = _gateway(client=client, config=_config(identity_propagation="full_context_token"))
    remote = {"a2a_version": A2A_VERSION, "id": "r", "params": {"route": "remote", "peer": "beta", "skill": "ping"}}

    await gateway.invoke(headers={"Authorization": "Bearer trusted"}, payload=remote)

    assert client.calls[0]["headers"]["Authorization"] == "Bearer trusted", "受信 token 必须透传到对端"


@pytest.mark.asyncio
async def test_fanout_limits_concurrency_and_isolates_single_failures():
    skill_gateway = _SkillGateway(delay=0.02)
    gateway, _, _ = _gateway(skill_gateway=skill_gateway, config=_config(max_fanout=1))

    payloads = [_local_payload(), {"params": {"route": "nowhere"}}, _local_payload(), _local_payload()]
    responses = await gateway.invoke_many(headers={"Authorization": "Bearer good"}, payloads=payloads)

    assert [item["status"] for item in responses] == ["SUCCEEDED", "DENIED", "SUCCEEDED", "SUCCEEDED"]
    assert responses[1]["error"]["code"] == "AI_A2A_ROUTE_FAILED", "单条失败不得放大为整体失败"
    assert skill_gateway.max_active == 1, "max_fanout=1 时必须串行"
    assert await gateway.invoke_many(headers={}, payloads=[]) == []


# ---------------------------------------------------------------- discovery


def _published_agent(tenant_id, name, *, snapshot=None, status="ACTIVE", version_status="PUBLISHED", description="d"):
    with SessionLocal.begin() as db:
        agent = Agent(tenant_id=tenant_id, name=name, description=description, status=status, source_type="USER")
        db.add(agent)
        db.flush()
        version = AgentVersion(
            tenant_id=tenant_id,
            agent_id=agent.id,
            version_number=1,
            status=version_status,
            config_snapshot=snapshot or {},
        )
        db.add(version)
        db.flush()
        agent.current_published_version_id = version.id
        agent_id, version_id = agent.id, version.id
    return agent_id, version_id


def test_discovery_projects_only_published_whitelisted_fields():
    tenant_id, _ = _tenant()
    other_tenant, _ = _tenant()
    published_id, published_version = _published_agent(
        tenant_id,
        "alpha",
        snapshot={
            "capabilities": ["chat", "vision"],
            "tags": ["t1"],
            "system_prompt": "SECRET PROMPT",
            "temperature": 0.3,
            "a2a": "enabled",
        },
    )
    _published_agent(tenant_id, "beta-draft", version_status="DRAFT")
    _published_agent(tenant_id, "gamma-inactive", status="ARCHIVED")
    _published_agent(other_tenant, "delta-other-tenant")

    agents = AgentDiscovery(FakeSdk()).list_agents(tenant_id=tenant_id)

    assert [item["name"] for item in agents] == ["alpha"], "只投影已发布版本 + ACTIVE Agent + 本租户"
    assert set(agents[0]) == {"agent_id", "agent_version_id", "name", "description", "version", "capabilities"}
    assert agents[0]["agent_id"] == str(published_id)
    assert agents[0]["agent_version_id"] == str(published_version)
    assert agents[0]["capabilities"] == ["chat", "enabled", "t1", "vision"], "能力按白名单键汇总并稳定排序"
    assert "SECRET PROMPT" not in str(agents), "提示词全文属于本地资产，绝不出现在发现结果里"


def test_discovery_projection_survives_closed_session():
    tenant_id, _ = _tenant()
    _published_agent(tenant_id, "alpha", snapshot={"capabilities": "chat"})

    agents = AgentDiscovery(FakeSdk()).list_agents(tenant_id=tenant_id)

    # 投影在会话内完成：会话关闭后仍是普通 dict/list，不带 ORM 脏状态
    assert agents == [
        {
            "agent_id": agents[0]["agent_id"],
            "agent_version_id": agents[0]["agent_version_id"],
            "name": "alpha",
            "description": "d",
            "version": 1,
            "capabilities": ["chat"],
        }
    ]


# ---------------------------------------------------------------- 观测接线（observability.sink）


# payload 键名出现这些片段即视为泄漏：凭据、头、请求/响应体都不属于观测面
_SENSITIVE_KEY_HINTS = (
    "token",
    "secret",
    "authorization",
    "header",
    "body",
    "content",
    "payload",
    "arguments",
    "cookie",
)


def _capture_events(monkeypatch) -> list[tuple[str, dict]]:
    """把 `peers.emit_event` 换成本地记录器，真实 sink 不参与。"""
    events: list[tuple[str, dict]] = []

    def _fake_emit(event: str, payload: dict) -> None:
        events.append((event, dict(payload)))

    monkeypatch.setattr(peers_module, "emit_event", _fake_emit)
    return events


def _assert_payload_whitelist(payload: dict) -> None:
    assert set(payload) == set(A2A_OUTBOUND_EVENT_FIELDS), f"payload keys drifted: {sorted(payload)}"
    for key in payload:
        lowered = key.lower()
        assert not any(hint in lowered for hint in _SENSITIVE_KEY_HINTS), f"sensitive key leaked: {key}"


@pytest.mark.asyncio
async def test_outbound_success_emits_exact_whitelist_payload(monkeypatch):
    events = _capture_events(monkeypatch)
    client = _Client(response=_Response(200, b'{"status":"SUCCEEDED","data":{"ok":true}}'))
    registry = _peer_registry(_config(), client=client)
    peer = PeerConfig(peer_id="beta", base_url="https://peer.test")

    body = await registry.invoke(
        peer,
        {"id": "1", "params": {"arguments": {"q": "secret-arg"}}},
        extra_headers={"Authorization": "Bearer evil-token", A2A_TRACE_HEADER: "t-1"},
        depth=1,
    )

    assert body["status"] == "SUCCEEDED"
    assert len(events) == 1, events
    name, payload = events[0]
    assert name == A2A_OUTBOUND_EVENT
    _assert_payload_whitelist(payload)

    assert payload["peer"] == "beta"
    assert payload["direction"] == A2A_DIRECTION_OUTBOUND
    assert payload["result"] == RESULT_OK
    assert payload["depth"] == 1
    assert payload["duration_ms"] >= 0
    assert payload["response_bytes"] == len(b'{"status":"SUCCEEDED","data":{"ok":true}}')

    dumped = json.dumps(payload)
    assert "evil-token" not in dumped, "token 原文绝不上报"
    assert "secret-arg" not in dumped, "请求体绝不上报"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error", "expected_result"),
    [
        (_Response(500, b"{}"), None, RESULT_ERROR),
        (_Response(403, b"{}"), None, RESULT_ERROR),
        (_Response(200, b"x" * 2000), None, RESULT_ERROR),
        (None, httpx.ReadTimeout("slow"), RESULT_TIMEOUT),
        (None, httpx.ConnectError("boom"), RESULT_ERROR),
    ],
)
async def test_outbound_failures_are_classified_in_event(monkeypatch, response, error, expected_result):
    events = _capture_events(monkeypatch)
    registry = _peer_registry(_config(), client=_Client(response=response, error=error))

    with pytest.raises(ApiError):
        await registry.invoke(PeerConfig(peer_id="beta", base_url="https://peer.test"), {"id": "1"}, depth=2)

    assert len(events) == 1, events
    name, payload = events[0]
    assert name == A2A_OUTBOUND_EVENT
    _assert_payload_whitelist(payload)
    assert payload["result"] == expected_result
    assert payload["depth"] == 2
    assert payload["response_bytes"] == 0, "失败路径不做响应体推断"


@pytest.mark.asyncio
async def test_circuit_open_is_reported_distinctly(monkeypatch):
    events = _capture_events(monkeypatch)
    registry = _peer_registry(_config(), client=_Client(response=_Response(500, b"{}")))
    peer = PeerConfig(peer_id="beta", base_url="https://peer.test")

    for _ in range(2):
        with pytest.raises(ApiError):
            await registry.invoke(peer, {"id": "1"})

    with pytest.raises(ApiError) as excinfo:
        await registry.invoke(peer, {"id": "1"})
    assert excinfo.value.code == CIRCUIT_OPEN_CODE

    assert [payload["result"] for _, payload in events] == [RESULT_ERROR, RESULT_ERROR, RESULT_CIRCUIT_OPEN]
    for _, payload in events:
        _assert_payload_whitelist(payload)


@pytest.mark.asyncio
async def test_gateway_remote_route_reports_outbound_depth(monkeypatch):
    """depth 由网关决定并透传到观测面：出站后深度应是入站 depth + 1。"""
    events = _capture_events(monkeypatch)
    client = _Client(response=_Response(200, b'{"status":"SUCCEEDED","data":{"pong":true}}'))
    gateway, _, _ = _gateway(client=client, config=_config(max_depth=2))
    remote = {"a2a_version": A2A_VERSION, "id": "r", "params": {"route": "remote", "peer": "beta", "skill": "ping"}}

    await gateway.invoke(headers={"Authorization": "Bearer good", A2A_DEPTH_HEADER: "1"}, payload=remote)

    assert len(events) == 1, events
    assert events[0][1]["depth"] == 2
    assert events[0][1]["peer"] == "beta"


@pytest.mark.asyncio
async def test_sink_failure_never_breaks_outbound_call(monkeypatch):
    """sink 抛错只记日志：成功路径照常返回，失败路径的异常语义也不变。"""

    def _boom(event: str, payload: dict) -> None:
        raise RuntimeError("sink down")

    monkeypatch.setattr(peers_module, "emit_event", _boom)
    peer = PeerConfig(peer_id="beta", base_url="https://peer.test")

    ok_registry = _peer_registry(_config(), client=_Client(response=_Response(200, b'{"status":"SUCCEEDED"}')))
    body = await ok_registry.invoke(peer, {"id": "1"}, depth=1)
    assert body["status"] == "SUCCEEDED", "观测旁路故障不得影响出站结果"

    bad_registry = _peer_registry(_config(), client=_Client(response=_Response(500, b"{}")))
    with pytest.raises(ApiError) as excinfo:
        await bad_registry.invoke(peer, {"id": "1"}, depth=1)
    assert excinfo.value.code == "AI_A2A_PEER_UNAVAILABLE", "观测旁路故障不得改写错误码"


# ---------------------------------------------------------------- 对端响应封装兼容


@pytest.mark.asyncio
async def test_unified_envelope_response_is_unwrapped():
    """对端按本项目统一响应返回时，A2A 响应在 data 里，必须解包再判 status。"""
    envelope = b'{"code":0,"message":"ok","data":{"status":"SUCCEEDED","data":{"echo":1}},"trace_id":"t"}'
    registry = _peer_registry(_config(), client=_Client(response=_Response(200, envelope)))

    result = await registry.invoke(PeerConfig(peer_id="beta", base_url="https://peer.test"), {"id": "1"}, depth=1)

    assert result["status"] == "SUCCEEDED"
    assert result["data"] == {"echo": 1}


@pytest.mark.asyncio
async def test_flat_response_is_not_unwrapped():
    """顶层已带 status 的响应不得再剥一层（避免把 data 里的业务字段误当信封）。"""
    flat = b'{"status":"SUCCEEDED","data":{"status":"nested-field","data":{}}}'
    registry = _peer_registry(_config(), client=_Client(response=_Response(200, flat)))

    result = await registry.invoke(PeerConfig(peer_id="beta", base_url="https://peer.test"), {"id": "1"}, depth=1)

    assert result["status"] == "SUCCEEDED"
    assert result["data"]["status"] == "nested-field"


# ---------------------------------------------------------------- 联邦入站（v1.4 §6.5 方案 B）


def _federated_config(tenant_id, *, enabled: bool = True, peer_secret: str = "cipher-1") -> A2AConfig:
    return _config(
        federation_enabled=enabled,
        remote_peers=(
            PeerConfig(
                peer_id="beta",
                base_url="https://peer.test",
                auth_secret_name=peer_secret,
                tenant_id=str(tenant_id),
                inbound_federation=True,
            ),
        ),
    )


def _federated_registry(sdk, config) -> FederatedPrincipalRegistry:
    registry = FederatedPrincipalRegistry.from_config(sdk, config)
    assert registry is not None
    return registry


def test_federation_disabled_builds_nothing():
    """开关关闭 → from_config 返回 None，不触碰 secret.provider（零行为变化）。"""
    secret = FakeSecretProvider({"cipher-1": "shared-secret"})
    FakeSdk(registry=FakeRegistry({"secret.provider": secret}))

    assert FederatedPrincipalRegistry.from_config(FakeSdk(), _federated_config(uuid4(), enabled=False)) is None
    assert secret.decrypt_calls == 0


def test_federation_enabled_without_principals_is_assembly_error():
    """开启联邦却不声明任何 inbound_federation peer = 配置矛盾，装配期拒绝。"""
    sdk = FakeSdk(registry=FakeRegistry({"secret.provider": FakeSecretProvider()}))
    config = _config(federation_enabled=True)

    with pytest.raises(Exception):
        FederatedPrincipalRegistry.from_config(sdk, config)


def test_federation_bad_secret_or_unknown_tenant_fail_at_assembly():
    """密钥解不开 / tenant 不存在都是身份事实不完整，必须装配期 fail fast。"""
    tenant_id, _user_id = _tenant()
    bad_secret_sdk = FakeSdk(registry=FakeRegistry({"secret.provider": FakeSecretProvider({})}))
    with pytest.raises(Exception):
        FederatedPrincipalRegistry.from_config(bad_secret_sdk, _federated_config(tenant_id, peer_secret="cipher-missing"))

    empty_secret_sdk = FakeSdk(registry=FakeRegistry({"secret.provider": FakeSecretProvider({"cipher-1": ""})}))
    with pytest.raises(Exception):
        FederatedPrincipalRegistry.from_config(empty_secret_sdk, _federated_config(tenant_id))

    missing_tenant_sdk = FakeSdk(registry=FakeRegistry({"secret.provider": FakeSecretProvider({"cipher-1": "s"})}))
    with pytest.raises(Exception):
        FederatedPrincipalRegistry.from_config(missing_tenant_sdk, _federated_config(uuid4()))


def test_federated_secret_grants_a2a_invoke_without_local_authz():
    """命中共享密钥 → 联邦主体（tenant=配置声明、user_id=None），不触本地身份/角色体系。"""
    tenant_id, user_id = _tenant()
    identity = _IdentityProvider(ctx=SecurityContext(tenant_id=tenant_id, user_id=user_id))
    authz = _AuthzProvider(allowed=True)
    sdk = FakeSdk(
        registry=FakeRegistry(
            {
                "secret.provider": FakeSecretProvider({"cipher-1": "shared-secret-xyz"}),
                "identity.provider": identity,
                "authz.provider": authz,
            }
        )
    )
    config = _federated_config(tenant_id)
    registry = _federated_registry(sdk, config)
    inbound = InboundIdentity(sdk, federated=registry)

    principal = inbound.authenticate("shared-secret-xyz", trace_id="trace-fed")

    assert principal.ctx.tenant_id == tenant_id
    assert principal.ctx.user_id is None, "联邦主体不是本地用户：合成 UUID 会撞 zr_audit_log 的 actor 外键"
    assert principal.principal_type == "federated_peer"
    assert principal.peer_id == "beta"
    assert identity.calls == [], "联邦命中不得再走本域 JWT"
    assert authz.calls == [], "凭据即授权（service account 语义），不走本地角色体系"


@pytest.mark.asyncio
async def test_gateway_federated_call_executes_and_audits_without_actor_fk():
    """网关级联邦入站：真实执行技能、审计行 actor 为 NULL（外键安全）、归属进 detail。"""
    skill_gateway = _SkillGateway(_Result(status="SUCCEEDED", data={"echo": 1}))
    tenant_id, _user_id = _tenant()
    sdk = FakeSdk(
        settings=SimpleNamespace(),
        registry=FakeRegistry({"secret.provider": FakeSecretProvider({"cipher-1": "shared-secret-xyz"})}),
        permissions=("db.query", "network.outbound"),
    )
    cfg = _federated_config(tenant_id)
    federated = FederatedPrincipalRegistry.from_config(sdk, cfg)
    gateway = A2AGateway(
        sdk,
        cfg,
        identity=InboundIdentity(sdk, federated=federated),
        discovery=AgentDiscovery(sdk),
        peers=PeerRegistry(sdk, cfg, guard=PluginGuardRegistry(failure_threshold=5, cooldown_seconds=30.0)),
        skill_gateway=skill_gateway,
    )

    response = await gateway.invoke(headers={"Authorization": "Bearer shared-secret-xyz"}, payload=_local_payload(arguments={}))

    assert response["status"] == "SUCCEEDED"
    assert skill_gateway.calls[0]["ctx"].user_id is None
    with SessionLocal() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == "A2A_INVOKED")).all()
    assert len(rows) == 1
    assert rows[0].actor_user_id is None, "审计 actor 不得写合成 UUID（zr_user 外键）"
    assert rows[0].detail["principal_type"] == "federated_peer"
    assert rows[0].detail["caller_peer_id"] == "beta"


def test_federated_miss_falls_back_to_local_jwt_without_double_audit():
    """密钥不符 → 静默回落本域 JWT（联邦侧不留痕）；本域拒绝只写一条 local_jwt 审计。"""
    tenant_id, user_id = _tenant()
    identity = _IdentityProvider(ctx=SecurityContext(tenant_id=tenant_id, user_id=user_id))
    authz = _AuthzProvider(allowed=False)
    sdk = FakeSdk(
        registry=FakeRegistry(
            {
                "secret.provider": FakeSecretProvider({"cipher-1": "shared-secret-xyz"}),
                "identity.provider": identity,
                "authz.provider": authz,
            }
        )
    )
    inbound = InboundIdentity(sdk, federated=_federated_registry(sdk, _federated_config(tenant_id)))

    with pytest.raises(ApiError) as excinfo:
        inbound.authenticate("not-the-secret", trace_id="trace-1")

    assert excinfo.value.code == "AI_A2A_DENIED"
    rows = _denial_rows(tenant_id)
    assert len(rows) == 1, "联邦未命中必须静默回落，不得先写一条联邦拒绝"
    assert rows[0].detail["principal_type"] == "local_jwt"


def test_federated_missing_token_denied_as_local_jwt():
    tenant_id, user_id = _tenant()
    identity = _IdentityProvider(ctx=SecurityContext(tenant_id=tenant_id, user_id=user_id))
    sdk = FakeSdk(
        registry=FakeRegistry(
            {
                "secret.provider": FakeSecretProvider({"cipher-1": "shared-secret-xyz"}),
                "identity.provider": identity,
            }
        )
    )
    inbound = InboundIdentity(sdk, federated=_federated_registry(sdk, _federated_config(tenant_id)))

    with pytest.raises(ApiError):
        inbound.authenticate(None)

    assert identity.calls == []


# ---------------------------------------------------------------- 出站 mTLS（v1.4 §6.5）


def test_peer_mtls_config_pairing_is_validated():
    """客户端证书与私钥必须成对声明；inbound_federation 必须给密钥与合法租户。"""
    with pytest.raises(ValueError):
        _config(
            remote_peers=(PeerConfig(peer_id="beta", base_url="https://peer.test", client_cert_secret_name="c1"),),
        ).validate()
    with pytest.raises(ValueError):
        _config(
            remote_peers=(
                PeerConfig(peer_id="beta", base_url="https://peer.test", inbound_federation=True, tenant_id="not-a-uuid"),
            ),
        ).validate()
    # 合法组合：cert+key 成对 + ca 独立
    _config(
        remote_peers=(
            PeerConfig(
                peer_id="beta",
                base_url="https://peer.test",
                client_cert_secret_name="c1",
                client_key_secret_name="k1",
                ca_bundle_secret_name="ca1",
            ),
        ),
    ).validate()


def _test_tls_material() -> dict[str, str]:
    """生成一份真实（自签）TLS 材料供单测用：_mtls_client_options 会真解析 PEM。"""
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    def _pem_key(key) -> str:
        return key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "unit-ca")])
    now = datetime.now(timezone.utc)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "unit-client")]))
        .issuer_name(ca_cert.subject)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .sign(ca_key, hashes.SHA256())
    )
    return {
        "cert": client_cert.public_bytes(serialization.Encoding.PEM).decode(),
        "key": _pem_key(client_key),
        "ca": ca_cert.public_bytes(serialization.Encoding.PEM).decode(),
    }


def test_mtls_material_decrypted_to_owner_only_temp_files_and_cleaned():
    """mTLS 材料解密 → SSLContext 构建（0600 临时文件）→ 请求后清理（密钥不驻留磁盘）。"""
    client = _Client(response=_Response(200, b'{"status":"SUCCEEDED"}'))
    material = _test_tls_material()
    sdk = FakeSdk(
        settings=SimpleNamespace(),
        registry=FakeRegistry(
            {
                "secret.provider": FakeSecretProvider(
                    {"cert-1": material["cert"], "key-1": material["key"], "ca-1": material["ca"]}
                ),
            }
        ),
    )
    config = _config()
    guard = PluginGuardRegistry(failure_threshold=2, cooldown_seconds=30.0)
    registry = PeerRegistry(sdk, config, guard=guard, client=client)
    peer = PeerConfig(
        peer_id="beta",
        base_url="https://peer.test",
        client_cert_secret_name="cert-1",
        client_key_secret_name="key-1",
        ca_bundle_secret_name="ca-1",
    )

    body = asyncio.run(registry.invoke(peer, {"id": "1"}))

    assert body["status"] == "SUCCEEDED"
    leftover = list(Path(tempfile.gettempdir()).glob("zt-a2a-mtls-*.pem"))
    assert leftover == [], "mTLS 临时材料必须在请求后清理"


def test_mtls_material_decrypt_failure_is_peer_unavailable():
    """证书/私钥解不开 → AI_A2A_PEER_UNAVAILABLE（对齐凭据语义），不发起出站。"""
    client = _Client(response=_Response(200, b'{"status":"SUCCEEDED"}'))
    sdk = FakeSdk(
        settings=SimpleNamespace(),
        registry=FakeRegistry({"secret.provider": FakeSecretProvider({})}),
    )
    registry = PeerRegistry(sdk, _config(), guard=PluginGuardRegistry(failure_threshold=2, cooldown_seconds=30.0), client=client)
    peer = PeerConfig(peer_id="beta", base_url="https://peer.test", client_cert_secret_name="cert-1", client_key_secret_name="key-1")

    with pytest.raises(ApiError) as excinfo:
        asyncio.run(registry.invoke(peer, {"id": "1"}))

    assert excinfo.value.code == "AI_A2A_PEER_UNAVAILABLE"
    assert client.calls == []
