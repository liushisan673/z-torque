"""A2A 联邦入站身份（v1.4 §6.5 方案 B：对端 service account 共享密钥）。

跨域联邦的最小落地形态：对端持有与本域配置一致的共享密钥（与出站
`auth_secret_name` 机制对称——同一密钥双向使用即成联邦），凭据即身份。

设计要点：
- 共享密钥经 secret.provider 在**装配期**预解密，明文只在本进程内存中
  （不落库、不进日志、不进审计、不进观测事件）；
- `match(token)`：逐条 `hmac.compare_digest`（先比长度再恒定时间比较）；
  命中返回 `FederatedPrincipal`，由 identity 层映射为
  `SecurityContext(tenant_id=配置声明, user_id=None)`——**联邦主体不是本地用户**，
  合成 UUID 会撞 `zr_audit_log.actor_user_id` 的 `zr_user` 外键（审计留痕是硬要求），
  因此归属走审计 detail（principal_type / peer_id），不走 actor 外键；
- 未命中返回 `None`、**不写审计**——调用方静默回落本域 JWT 通道，避免
  "JWT 失败被误记成联邦拒绝"的双留痕；
- 权限语义：持有共享密钥 = 该对端被明确授权 `a2a.invoke`（service account 语义），
  不走本地角色体系；后续技能级 required_permissions 由内核 permission_checker
  default-deny（user_id=None + 非空权限集 → 拒绝），联邦主体默认无本地授权；
- 入站真 mTLS 属部署层职责（uvicorn 不向 ASGI 应用暴露客户端证书），见 §12.5；
  应用层只信"凭据事实"，不信任何身份头。
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.plugin_sdk import AssemblyError


@dataclass(frozen=True)
class FederatedPrincipal:
    """一条入站联邦主体：peer 身份 + 租户事实 + 已解密的共享密钥（仅内存）。"""

    peer_id: str
    tenant_id: UUID
    expected_secret: str


class FederatedPrincipalRegistry:
    """入站联邦主体集合（方案 B）。未命中一律返回 None，绝不抛错、不留痕。"""

    def __init__(self, principals: tuple[FederatedPrincipal, ...]) -> None:
        self._principals = principals

    @classmethod
    def from_config(cls, sdk: Any, config: Any) -> "FederatedPrincipalRegistry | None":
        """装配期构造：`a2a_federation_enabled=false` → None（零行为变化）。

        开启即 fail fast：无任何 `inbound_federation` peer = 配置矛盾；密钥解不开 /
        tenant 不存在 = 身份事实不完整，都不允许带病启动。
        """
        if not getattr(config, "federation_enabled", False):
            return None
        declared = [peer for peer in config.remote_peers if peer.inbound_federation]
        if not declared:
            raise AssemblyError("a2a_federation_enabled=true requires at least one peer with inbound_federation=true")
        # contribute 期 Port 注册表尚未 resolve：secret.provider 按装配期约定走对象共享
        # （SecretProviderPlugin.contribute 会 publish 实例），registry 仅作兜底
        provider = sdk.shared("secret.provider")
        if provider is None:
            provider = sdk.registry.try_get("secret.provider")
        if provider is None:
            raise AssemblyError("a2a federation requires secret.provider implementation")
        known_tenants = {str(item) for item in (sdk.list_tenant_ids() or [])}
        principals: list[FederatedPrincipal] = []
        for peer in declared:
            plaintext = _decrypt_secret(provider, peer.peer_id, peer.auth_secret_name)
            tenant_id = UUID(peer.tenant_id)
            if str(tenant_id) not in known_tenants:
                raise AssemblyError(f"a2a federation peer {peer.peer_id!r} declares unknown tenant {peer.tenant_id!r}")
            principals.append(FederatedPrincipal(peer_id=peer.peer_id, tenant_id=tenant_id, expected_secret=plaintext))
        return cls(principals=tuple(principals))

    @property
    def principal_ids(self) -> tuple[str, ...]:
        """已装配的联邦 peer id（用于测试与诊断，不含任何密钥材料）。"""
        return tuple(item.peer_id for item in self._principals)

    def match(self, token: str) -> FederatedPrincipal | None:
        """凭据匹配：命中返回联邦主体；未命中返回 None（静默回落 JWT 通道）。"""
        if not token:
            return None
        candidate = token.encode("utf-8")
        for principal in self._principals:
            expected = principal.expected_secret.encode("utf-8")
            # 长度不等的 compare_digest 也能安全返回 False，但先短路可省去逐字节比较
            if len(candidate) != len(expected):
                continue
            if hmac.compare_digest(candidate, expected):
                return principal
        return None


def _decrypt_secret(provider: Any, peer_id: str, secret_name: str) -> str:
    """解密共享密钥材料（与 peers._auth_headers 同一引用语义：可带 `secret://` 前缀）。"""
    ciphertext = secret_name[len("secret://") :] if secret_name.startswith("secret://") else secret_name
    try:
        plaintext = provider.decrypt(ciphertext)
    except Exception as exc:  # noqa: BLE001 - 装配期解密失败必须 fail fast，不留半可用主体
        raise AssemblyError(f"a2a federation peer {peer_id!r} shared secret cannot be decrypted") from exc
    if not plaintext:
        raise AssemblyError(f"a2a federation peer {peer_id!r} shared secret is empty")
    return plaintext

