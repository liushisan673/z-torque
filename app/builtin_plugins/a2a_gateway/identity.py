"""入站身份与鉴权（v1.3 §6.5，v1 仅方案 A：本域 JWT）。

调用方携带**本域** JWT → `identity.provider.authenticate(db, token)` → 得 `SecurityContext`
→ `authz.provider.has_all(db, user_id, ["a2a.invoke"])`。两个 Port 都吃 Session，
因此必须用 `sdk.session()` 的短事务；认证与鉴权共用同一事务，执行阶段另开事务，
禁止跨 await 持有 Session。

红线：
- **不得**信任裸 `X-User-Id` 头（该头只用于出站携带身份）；
- 任何拒绝都先 `sdk.record_audit(A2A_DENIED)` 再抛错（业务事务回滚不影响留痕）。
"""

from __future__ import annotations

from typing import Any

from app.core.errors import ApiError
from app.plugin_sdk import SecurityContext

A2A_INVOKE_PERMISSION = "a2a.invoke"
_RESOURCE_TYPE = "a2a_call"


class InboundIdentity:
    """本域 JWT 入站认证 + `a2a.invoke` 鉴权（唯一支持的身份方案）。"""

    def __init__(self, sdk: Any, *, permission: str = A2A_INVOKE_PERMISSION) -> None:
        self._sdk = sdk
        self._permission = permission

    def _providers(self) -> tuple[Any, Any]:
        identity = self._sdk.registry.try_get("identity.provider")
        authz = self._sdk.registry.try_get("authz.provider")
        if identity is None or authz is None:
            raise ApiError("AI_CAPABILITY_UNAVAILABLE", "identity.provider / authz.provider is not assembled", 503)
        return identity, authz

    def authenticate(self, token: str | None, *, trace_id: str | None = None) -> SecurityContext:
        """校验入站凭据并要求 `a2a.invoke`；失败即拒绝并留痕。"""
        if not token:
            self._deny(None, "missing bearer token", trace_id=trace_id)
        identity, authz = self._providers()
        with self._sdk.session() as db:
            try:
                ctx = identity.authenticate(db, token)
            except Exception as exc:  # noqa: BLE001 - 过期/异域/伪造一律等价于拒绝
                self._deny(None, f"inbound authentication failed: {exc}", trace_id=trace_id)
            if ctx is None or ctx.user_id is None:
                self._deny(None, "inbound authentication failed", trace_id=trace_id)
            if not authz.has_all(db, ctx.user_id, [self._permission]):
                self._deny(ctx, f"missing {self._permission}", trace_id=trace_id)
            return SecurityContext(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                session_id=getattr(ctx, "session_id", None),
                trace_id=trace_id or getattr(ctx, "trace_id", None),
            )

    def _deny(self, ctx: SecurityContext | None, reason: str, *, trace_id: str | None) -> None:
        if ctx is not None:
            self._sdk.record_audit(
                tenant_id=ctx.tenant_id,
                actor_user_id=ctx.user_id,
                action="A2A_DENIED",
                resource_type=_RESOURCE_TYPE,
                resource_id=None,
                summary=f"a2a inbound denied: {reason}",
                detail={"reason": reason},
                trace_id=trace_id,
            )
        raise ApiError("AI_A2A_DENIED", "A2A inbound authentication or authorization failed", 403)
