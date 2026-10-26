"""A2A 插件配置（v1.3 §6.4）。

配置唯一来源为全局 Settings（env / .env），插件只经 `PluginSdk.settings` 读取。
校验在 `contribute()` 内完成（`config_schema` 不自动校验）：非法配置 → `AssemblyError`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

IDENTITY_NONE = "none"
IDENTITY_USER_HEADER = "user_id_header"
IDENTITY_FULL_CONTEXT = "full_context_token"
IDENTITY_MODES = (IDENTITY_NONE, IDENTITY_USER_HEADER, IDENTITY_FULL_CONTEXT)


@dataclass(frozen=True)
class PeerConfig:
    """远端 peer：`auth_secret_name` 指向可 decrypt 的密钥材料，不是 `secret://`。

    v1.4 M4 扩展：
    - 联邦入站（方案 B）：`inbound_federation=true` 时该 peer 的共享密钥同时用于
      入站 service account 认证（与出站 Bearer 对称），`tenant_id` 声明主体归属租户；
    - 出站 mTLS：`client_cert_secret_name` / `client_key_secret_name` 成对出现，
      `ca_bundle_secret_name` 可独立配置（校验对端服务端证书）。
    """

    peer_id: str
    base_url: str
    auth_secret_name: str = ""
    timeout_seconds: float | None = None
    tenant_id: str = ""
    inbound_federation: bool = False
    client_cert_secret_name: str = ""
    client_key_secret_name: str = ""
    ca_bundle_secret_name: str = ""

    @property
    def is_loopback(self) -> bool:
        host = (urlparse(self.base_url).hostname or "").lower()
        return host in ("127.0.0.1", "::1", "localhost")

    @property
    def mtls_enabled(self) -> bool:
        return bool(self.client_cert_secret_name and self.client_key_secret_name)


@dataclass(frozen=True)
class A2AConfig:
    enabled: bool
    local_discovery: bool
    max_depth: int
    max_fanout: int
    default_timeout_seconds: float
    identity_propagation: str
    max_response_bytes: int
    remote_peers: tuple[PeerConfig, ...] = ()
    allowed_peers: frozenset[str] = frozenset()
    circuit_failure_threshold: int = 5
    circuit_cooldown_seconds: float = 30.0
    federation_enabled: bool = False

    @classmethod
    def from_settings(cls, settings: Any) -> "A2AConfig":
        peers: list[PeerConfig] = []
        for raw in getattr(settings, "a2a_peer_configs", []) or []:
            peer_id = str(raw.get("id") or raw.get("peer_id") or "").strip()
            base_url = str(raw.get("base_url") or "").strip()
            if not peer_id or not base_url:
                continue
            timeout = raw.get("timeout_seconds")
            peers.append(
                PeerConfig(
                    peer_id=peer_id,
                    base_url=base_url.rstrip("/"),
                    auth_secret_name=str(raw.get("auth_secret_name") or raw.get("token_ref") or ""),
                    timeout_seconds=float(timeout) if timeout not in (None, "") else None,
                    tenant_id=str(raw.get("tenant_id") or "").strip(),
                    inbound_federation=bool(raw.get("inbound_federation") or False),
                    client_cert_secret_name=str(raw.get("client_cert_secret_name") or "").strip(),
                    client_key_secret_name=str(raw.get("client_key_secret_name") or "").strip(),
                    ca_bundle_secret_name=str(raw.get("ca_bundle_secret_name") or "").strip(),
                )
            )
        return cls(
            enabled=bool(getattr(settings, "a2a_enabled", False)),
            local_discovery=bool(getattr(settings, "a2a_local_discovery", True)),
            max_depth=int(getattr(settings, "a2a_max_depth", 2)),
            max_fanout=int(getattr(settings, "a2a_max_fanout", 5)),
            default_timeout_seconds=float(getattr(settings, "a2a_default_timeout_seconds", 120.0)),
            identity_propagation=str(getattr(settings, "a2a_identity_propagation", IDENTITY_USER_HEADER) or IDENTITY_USER_HEADER).lower(),
            max_response_bytes=int(getattr(settings, "a2a_max_response_bytes", 1_000_000)),
            remote_peers=tuple(peers),
            allowed_peers=frozenset(str(item).strip() for item in (getattr(settings, "a2a_allowed_peers", []) or []) if str(item).strip()),
            circuit_failure_threshold=int(getattr(settings, "a2a_circuit_failure_threshold", 5)),
            circuit_cooldown_seconds=float(getattr(settings, "a2a_circuit_cooldown_seconds", 30.0)),
            federation_enabled=bool(getattr(settings, "a2a_federation_enabled", False)),
        )

    def peer(self, peer_id: str) -> PeerConfig | None:
        for item in self.remote_peers:
            if item.peer_id == peer_id:
                return item
        return None

    def validate(self) -> None:
        """装配期校验：任何一条不满足都拒绝启动（不做运行期降级）。"""
        if self.identity_propagation not in IDENTITY_MODES:
            raise ValueError(f"a2a_identity_propagation must be one of {IDENTITY_MODES}")
        if not 1 <= self.max_depth <= 8:
            raise ValueError("a2a_max_depth must be within 1..8")
        if self.max_fanout < 1:
            raise ValueError("a2a_max_fanout must be >= 1")
        if self.default_timeout_seconds <= 0:
            raise ValueError("a2a_default_timeout_seconds must be positive")
        if self.max_response_bytes < 1024:
            raise ValueError("a2a_max_response_bytes must be >= 1024")
        if self.circuit_failure_threshold < 1 or self.circuit_cooldown_seconds < 1:
            raise ValueError("a2a circuit breaker thresholds must be positive")
        # 白名单语义：声明了远端 peer 却不给白名单 = 默认全开，必须装配期失败
        if self.remote_peers and not self.allowed_peers:
            raise ValueError("a2a_allowed_peers must not be empty when a2a_remote_peers is configured")
        seen: set[str] = set()
        for peer in self.remote_peers:
            if peer.peer_id in seen:
                raise ValueError(f"duplicate a2a peer id: {peer.peer_id}")
            seen.add(peer.peer_id)
            if peer.peer_id not in self.allowed_peers:
                raise ValueError(f"a2a peer {peer.peer_id!r} is not listed in a2a_allowed_peers")
            parsed = urlparse(peer.base_url)
            if parsed.scheme not in ("https", "http"):
                raise ValueError(f"a2a peer {peer.peer_id!r} base_url must be http(s)")
            # 出站强制 https：回环地址允许 http 便于本地联调
            if parsed.scheme == "http" and not peer.is_loopback:
                raise ValueError(f"a2a peer {peer.peer_id!r} base_url must use https (only loopback may use http)")
            if not parsed.netloc:
                raise ValueError(f"a2a peer {peer.peer_id!r} base_url is not a valid URL")
            # 联邦入站（方案 B）：共享密钥与租户事实缺一不可（tenant 存在性由插件装配期查库校验）
            if peer.inbound_federation:
                if not peer.auth_secret_name:
                    raise ValueError(f"a2a peer {peer.peer_id!r} inbound_federation requires auth_secret_name")
                try:
                    UUID(peer.tenant_id)
                except ValueError as exc:
                    raise ValueError(f"a2a peer {peer.peer_id!r} inbound_federation requires a valid tenant_id UUID") from exc
            # 出站 mTLS：客户端证书与私钥必须成对声明；CA bundle 可独立
            if bool(peer.client_cert_secret_name) != bool(peer.client_key_secret_name):
                raise ValueError(f"a2a peer {peer.peer_id!r} mTLS requires client_cert_secret_name and client_key_secret_name together")
