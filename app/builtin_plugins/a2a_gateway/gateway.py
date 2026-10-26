"""A2A 网关：深度/扇出/熔断与路由编排（v1.3 §6.6）。

职责边界（v1.3 §6.2）：
- 本模块**不触 DB**（身份在 identity.py、发现信息在 discovery.py）；
- 本地路由只委托 `skill.executor`（SkillGateway），不写父子 Task 字段、不建 Task；
- 远端路由经 peers.py 出站（不复用 connector）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from app.builtin_plugins.a2a_gateway.config import (
    IDENTITY_FULL_CONTEXT,
    IDENTITY_NONE,
    IDENTITY_USER_HEADER,
    A2AConfig,
)
from app.builtin_plugins.a2a_gateway.discovery import AgentDiscovery
from app.builtin_plugins.a2a_gateway.identity import InboundIdentity, InboundPrincipal
from app.builtin_plugins.a2a_gateway.peers import PeerRegistry
from app.builtin_plugins.a2a_gateway.protocol import (
    A2A_DEPTH_HEADER,
    A2A_ROUTE_HEADER,
    A2A_TRACE_HEADER,
    ROUTE_LOCAL,
    ROUTE_REMOTE,
    STATUS_DENIED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    A2ARequest,
    build_response,
    parse_depth,
    parse_request,
    response_from_result,
)
from app.core.errors import ApiError
from app.plugin_sdk import SecurityContext

_BREAKER = "plugin.a2a_gateway"
_RESOURCE_TYPE = "a2a_call"
_DENIED_CODES = frozenset({"AI_A2A_DENIED", "AI_A2A_DEPTH_EXCEEDED", "AI_PERMISSION_DENIED"})


def _bearer(headers: Mapping[str, str]) -> str | None:
    for key, value in headers.items():
        if key.lower() != "authorization":
            continue
        scheme, _, token = str(value).partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
    return None


class A2AGateway:
    """A2A 会话编排：入站认证 → 深度 → 鉴权 → 路由 → 标准化 → 审计。"""

    def __init__(
        self,
        sdk: Any,
        config: A2AConfig,
        *,
        identity: InboundIdentity,
        discovery: AgentDiscovery,
        peers: PeerRegistry,
        skill_gateway: Any,
        guard: Any = None,
    ) -> None:
        self._sdk = sdk
        self._config = config
        self.identity = identity
        self.discovery = discovery
        self.peers = peers
        self._skill_gateway = skill_gateway
        self._guard = guard
        self._semaphore = asyncio.Semaphore(config.max_fanout)

    # ---- 入口 ----
    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def is_local_discovery_enabled(self) -> bool:
        return self._config.enabled and self._config.local_discovery

    def require_enabled(self) -> None:
        if not self._config.enabled:
            raise ApiError("AI_A2A_DISABLED", "A2A gateway is disabled (A2A_ENABLED=false)", 503)

    async def invoke(self, *, headers: Mapping[str, str], payload: Any, trace_id: str | None = None) -> dict[str, Any]:
        self.require_enabled()
        if self._guard is not None:
            self._guard.check(_BREAKER)
        token = _bearer(headers)
        principal = self.identity.authenticate(token, trace_id=trace_id)
        ctx = principal.ctx
        request = parse_request(payload)
        depth = parse_depth(headers, max_depth=self._config.max_depth)
        try:
            if request.route == ROUTE_REMOTE:
                response = await self._invoke_remote(request, ctx=ctx, depth=depth, headers=headers)
            else:
                response = await self._invoke_local(request, ctx=ctx, depth=depth)
        except ApiError as exc:
            self._record(principal, action="A2A_DENIED" if exc.code in _DENIED_CODES else "A2A_FAILED", summary=exc.message, code=exc.code, depth=depth, peer=request.peer)
            if self._guard is not None:
                self._guard.record(_BREAKER, succeeded=False)
            raise
        self._record(
            principal,
            action="A2A_INVOKED" if response.get("status") == STATUS_SUCCEEDED else "A2A_FAILED",
            summary=f"a2a {request.route} call {response.get('status')}",
            code=(response.get("error") or {}).get("code"),
            depth=depth,
            peer=request.peer,
        )
        if self._guard is not None:
            self._guard.record(_BREAKER, succeeded=response.get("status") == STATUS_SUCCEEDED)
        return response

    async def invoke_many(self, *, headers: Mapping[str, str], payloads: list[Any], trace_id: str | None = None) -> list[Any]:
        """扇出：`asyncio.Semaphore(max_fanout)` 限并发，超限排队而非静默丢弃。"""
        self.require_enabled()
        if not payloads:
            return []
        # 入站身份只校验一次：扇出的每个子调用共享同一调用方上下文
        self.identity.authenticate(_bearer(headers), trace_id=trace_id)

        async def _one(item: Any) -> Any:
            async with self._semaphore:
                request_id = str(item.get("id") or "") if isinstance(item, dict) else ""
                try:
                    return await self.invoke(headers=headers, payload=item, trace_id=trace_id)
                except ApiError as exc:
                    # 扇出中单个子调用失败不放大为整体失败：按条返回 DENIED/FAILED
                    return build_response(request_id=request_id, status=STATUS_DENIED, error_code=exc.code, error_message=exc.message)

        return list(await asyncio.gather(*(_one(item) for item in payloads)))

    # ---- 本地 ----
    async def _invoke_local(self, request: A2ARequest, *, ctx: SecurityContext, depth: int) -> dict[str, Any]:
        if request.skill_version_id is None:
            raise ApiError("AI_A2A_ROUTE_FAILED", "local route requires skill_version_id", 404)
        with self._sdk.session() as db:
            if hasattr(self._skill_gateway, "execute_async"):
                result = await self._skill_gateway.execute_async(
                    db,
                    ctx=ctx,
                    skill_version_id=request.skill_version_id,
                    arguments=dict(request.arguments or {}),
                    timeout_seconds=self._config.default_timeout_seconds,
                )
            else:  # pragma: no cover - 兼容只实现同步 execute 的 Port 实现
                result = await asyncio.to_thread(
                    self._skill_gateway.execute,
                    db,
                    ctx=ctx,
                    skill_version_id=request.skill_version_id,
                    arguments=dict(request.arguments or {}),
                    timeout_seconds=self._config.default_timeout_seconds,
                )
            return response_from_result(request_id=request.request_id, result=result, depth=depth)

    # ---- 远端 ----
    async def _invoke_remote(self, request: A2ARequest, *, ctx: SecurityContext, depth: int, headers: Mapping[str, str]) -> dict[str, Any]:
        peer_id = request.peer or ""
        peer = self.peers.resolve(peer_id)
        outbound_depth = depth + 1
        if outbound_depth > self._config.max_depth:
            raise ApiError("AI_A2A_DEPTH_EXCEEDED", f"outbound depth {outbound_depth} exceeds max_depth {self._config.max_depth}", 400)
        payload = request.outbound_payload()
        payload["depth"] = outbound_depth
        if request.skill_version_id is not None:
            # 出站载荷只描述"对端要执行什么"：调用方已经钉死了具体已发布技能版本，
            # 对端应当直接执行（route=local），而不是拿**发送方命名空间**里的 peer id
            # 再跳一次。真联调实测：原样转发 "params.peer=b" 会让对端去解析它自己的
            # peer 表，结果 503/打错人（peer id 是各部署自己的名字，不是全局标识）。
            payload["params"]["route"] = ROUTE_LOCAL
        extra = {A2A_DEPTH_HEADER: str(outbound_depth), A2A_ROUTE_HEADER: "outbound"}
        if ctx.trace_id:
            extra[A2A_TRACE_HEADER] = ctx.trace_id
        body = await self.peers.invoke(
            peer,
            payload,
            extra_headers=extra,
            identity_headers=self._propagation_headers(ctx, headers),
            # 只用于观测：出站深度由网关决定，peers 不自行推导
            depth=outbound_depth,
        )
        status = str(body.get("status") or STATUS_FAILED)
        if status == STATUS_SUCCEEDED:
            return build_response(request_id=request.request_id, status=STATUS_SUCCEEDED, data=body.get("data"), peer=peer_id, depth=outbound_depth)
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        return build_response(
            request_id=request.request_id,
            status=STATUS_DENIED if status == STATUS_DENIED else STATUS_FAILED,
            error_code=str(error.get("code") or "AI_A2A_PEER_UNAVAILABLE"),
            error_message=str(error.get("message") or "remote peer returned failure"),
            peer=peer_id,
            depth=outbound_depth,
        )

    def _propagation_headers(self, ctx: SecurityContext, headers: Mapping[str, str]) -> dict[str, str]:
        mode = self._config.identity_propagation
        if mode == IDENTITY_NONE:
            return {}
        if mode == IDENTITY_FULL_CONTEXT:
            token = _bearer(headers)
            return {"Authorization": f"Bearer {token}"} if token else {}
        if mode == IDENTITY_USER_HEADER:
            return {"X-User-Id": str(ctx.user_id)} if ctx.user_id else {}
        return {}

    # ---- 审计 ----
    def _record(self, principal: InboundPrincipal, *, action: str, summary: str, code: str | None, depth: int, peer: str | None) -> None:
        ctx = principal.ctx
        detail = {"depth": depth, "error_code": code, "principal_type": principal.principal_type}
        if peer:
            detail["peer"] = peer
        if principal.peer_id:
            # 联邦主体归属（ctx.user_id 恒为 None，不走 actor 外键）
            detail["caller_peer_id"] = principal.peer_id
        # 经 SDK 独立事务留痕（插件不 import 内核审计实现）
        self._sdk.record_audit(
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            action=action,
            resource_type=_RESOURCE_TYPE,
            resource_id=None,
            summary=summary,
            detail=detail,
            trace_id=ctx.trace_id,
        )
