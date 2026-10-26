"""入站身份与鉴权（v1.3 §6.5 方案 A：本域 JWT；v1.4 §6.5 追加方案 B：联邦共享密钥）。

调用方携带**本域** JWT → `identity.provider.authenticate(db, token)` → 得 `SecurityContext`
→ `authz.provider.has_all(db, user_id, ["a2a.invoke"])`。两个 Port 都吃 Session，
因此必须用 `sdk.session()` 的短事务；认证与鉴权共用同一事务，执行阶段另开事务，
禁止跨 await 持有 Session。

联邦通道（方案 B，`a2a_federation_enabled=true` 时装配）：对端以共享密钥作为
Bearer 凭据（service account 语义），`FederatedPrincipalRegistry.authenticate`
先于本域 JWT 尝试——共享密钥是高熵随机串，先试联邦可避免"JWT 失败先误写一条
A2A_DENIED 再回落"的双留痕。命中即视为已授权 `a2a.invoke`（凭据即授权，
service account 不在本地角色体系内）；未命中返回 None，静默回落 JWT。

红线：
- **不得**信任裸 `X-User-Id` 头（该头只用于出站携带身份）；联邦通道同样只信
  Bearer 凭据事实，不信任何身份头；
- 任何拒绝都先 `sdk.record_audit(A2A_DENIED)` 再抛错（业务事务回滚不影响留痕），
  detail 带 `principal_type`（local_jwt / federated_peer）便于分辨拒绝来源。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.errors import ApiError
from app.plugin_sdk import SecurityContext

if TYPE_CHECKING:
    from app.builtin_plugins.a2a_gateway.federated import FederatedPrincipalRegistry

A2A_INVOKE_PERMISSION = "a2a.invoke"
_RESOURCE_TYPE = "a2a_call"
_PRINCIPAL_LOCAL_JWT = "local_jwt"
_PRINCIPAL_FEDERATED = "federated_peer"


@dataclass(frozen=True)
class InboundPrincipal:
    """入站主体：SecurityContext + 归属事实（供网关审计 detail 归属）。

    联邦主体的 `ctx.user_id` 恒为 None（不是本地用户，合成 UUID 会撞审计外键），
    归属信息在 principal_type / peer_id 上。
    """

    ctx: SecurityContext
    principal_type: str
    peer_id: str | None = None


class InboundIdentity:
    """入站认证 + `a2a.invoke` 鉴权：联邦共享密钥（可选）→ 本域 JWT（兜底）。"""

    def __init__(self, sdk: Any, *, permission: str = A2A_INVOKE_PERMISSION, federated: "FederatedPrincipalRegistry | None" = None) -> None:
        self._sdk = sdk
        self._permission = permission
        self._federated = federated

    def _providers(self) -> tuple[Any, Any]:
        identity = self._sdk.registry.try_get("identity.provider")
        authz = self._sdk.registry.try_get("authz.provider")
        if identity is None or authz is None:
            raise ApiError("AI_CAPABILITY_UNAVAILABLE", "identity.provider / authz.provider is not assembled", 503)
        return identity, authz

    def authenticate(self, token: str | None, *, trace_id: str | None = None) -> InboundPrincipal:
        """校验入站凭据并要求 `a2a.invoke`；失败即拒绝并留痕。"""
        if not token:
            self._deny(None, "missing bearer token", principal_type=_PRINCIPAL_LOCAL_JWT, trace_id=trace_id)
        if self._federated is not None:
            matched = self._federated.match(token)
            if matched is not None:
                # 凭据即授权（service account 语义）：共享密钥命中 = 该对端被明确
                # 授权 a2a.invoke，不走本地角色体系。user_id=None：联邦主体不是本地
                # 用户，审计归属由 principal_type/peer_id 承载（detail）。
                return InboundPrincipal(
                    ctx=SecurityContext(tenant_id=matched.tenant_id, user_id=None, session_id=None, trace_id=trace_id),
                    principal_type=_PRINCIPAL_FEDERATED,
                    peer_id=matched.peer_id,
                )
        identity, authz = self._providers()
        with self._sdk.session() as db:
            try:
                ctx = identity.authenticate(db, token)
            except Exception as exc:  # noqa: BLE001 - 过期/异域/伪造一律等价于拒绝
                self._deny(None, f"inbound authentication failed: {exc}", principal_type=_PRINCIPAL_LOCAL_JWT, trace_id=trace_id)
            if ctx is None or ctx.user_id is None:
                self._deny(None, "inbound authentication failed", principal_type=_PRINCIPAL_LOCAL_JWT, trace_id=trace_id)
            if not authz.has_all(db, ctx.user_id, [self._permission]):
                self._deny(ctx, f"missing {self._permission}", principal_type=_PRINCIPAL_LOCAL_JWT, trace_id=trace_id)
            return InboundPrincipal(
                ctx=SecurityContext(
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    session_id=getattr(ctx, "session_id", None),
                    trace_id=trace_id or getattr(ctx, "trace_id", None),
                ),
                principal_type=_PRINCIPAL_LOCAL_JWT,
            )

    def _deny(self, ctx: SecurityContext | None, reason: str, *, principal_type: str, trace_id: str | None) -> None:
        if ctx is not None:
            detail: dict[str, Any] = {"reason": reason, "principal_type": principal_type}
            self._sdk.record_audit(
                tenant_id=ctx.tenant_id,
                actor_user_id=ctx.user_id,
                action="A2A_DENIED",
                resource_type=_RESOURCE_TYPE,
                resource_id=None,
                summary=f"a2a inbound denied: {reason}",
                detail=detail,
                trace_id=trace_id,
            )
        raise ApiError("AI_A2A_DENIED", "A2A inbound authentication or authorization failed", 403)
