"""远端 peer 白名单与出站 HTTP（v1.3 §6.6）。

硬约束：
- 每次出站前 `sdk.require_permission("network.outbound")`（未声明即 PermissionError）；
- `allowed_peers` 白名单之外一律 `AI_A2A_PEER_UNAVAILABLE`（空 = 全部拒绝）；
- 强制 https（回环可 http，装配期已校验）、`follow_redirects=False`；
- 响应体读取到 `max_response_bytes` 立即中断并失败，**不截断后解析**；
- 错误映射：4xx→`AI_A2A_DENIED`、超时→`AI_A2A_TIMEOUT`、连接失败/5xx/超限→`AI_A2A_PEER_UNAVAILABLE`。

观测：所有出站调用在**唯一收口点** `PeerRegistry.invoke` 上报 `a2a.outbound`
（peer / direction / result / duration_ms / response_bytes / depth）。
payload 白名单刻意不含 token、header 原文与请求/响应 body——那些属于对端资产。
上报失败只记日志，绝不影响出站结果与异常语义。
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urljoin

from app.builtin_plugins.a2a_gateway.config import A2AConfig, PeerConfig
from app.core.errors import ApiError
from app.plugin_sdk.observability import emit_event

_BREAKER_PREFIX = "plugin.a2a_gateway.peer:"
_INVOKE_PATH = "/api/z-torque/a2a/invoke"
# 熔断快速失败的错误码（与内核 plugin_guard 的常量同值；插件不得 import 内核模块，故本地复述）
_CIRCUIT_OPEN_CODE = "AI_PLUGIN_CIRCUIT_OPEN"
_TIMEOUT_CODE = "AI_A2A_TIMEOUT"

_LOGGER = logging.getLogger(__name__)

# ---- observability.sink 事件契约（事件名 / 方向 / 结果 / payload 白名单）----
A2A_OUTBOUND_EVENT = "a2a.outbound"
A2A_OUTBOUND_EVENT_FIELDS: tuple[str, ...] = (
    "peer",
    "direction",
    "result",
    "duration_ms",
    "response_bytes",
    "depth",
)
A2A_DIRECTION_OUTBOUND = "outbound"
RESULT_OK = "ok"
RESULT_ERROR = "error"
RESULT_TIMEOUT = "timeout"
RESULT_CIRCUIT_OPEN = "circuit_open"


def _emit_outbound(peer_id: str, *, result: str, duration_ms: int, response_bytes: int, depth: int | None) -> None:
    """上报一次出站调用结果（payload 严格按 `A2A_OUTBOUND_EVENT_FIELDS` 构造）。"""
    payload: dict[str, Any] = {
        "peer": str(peer_id),
        "direction": A2A_DIRECTION_OUTBOUND,
        "result": result,
        "duration_ms": max(0, int(duration_ms)),
        "response_bytes": max(0, int(response_bytes)),
        "depth": int(depth) if depth is not None else None,
    }
    try:
        emit_event(A2A_OUTBOUND_EVENT, payload)
    except Exception:  # noqa: BLE001 - 观测上报失败不得影响出站主流程
        _LOGGER.warning(
            "observability sink failed for event %s (peer %s, result %s)",
            A2A_OUTBOUND_EVENT,
            payload["peer"],
            result,
            exc_info=True,
        )


class PeerRegistry:
    """peer 白名单 + 出站调用（不覆用 connector：connector 是 MCP 专用）。"""

    def __init__(self, sdk: Any, config: A2AConfig, *, guard: Any = None, client: Any = None) -> None:
        self._sdk = sdk
        self._config = config
        self._guard = guard
        self._client = client

    # ---- 白名单 / 熔断 ----
    def resolve(self, peer_id: str) -> PeerConfig:
        if peer_id not in self._config.allowed_peers:
            raise ApiError("AI_A2A_PEER_UNAVAILABLE", f"peer {peer_id!r} is not in a2a_allowed_peers", 503)
        peer = self._config.peer(peer_id)
        if peer is None:
            raise ApiError("AI_A2A_ROUTE_FAILED", f"peer {peer_id!r} is not configured", 404)
        return peer

    def check_breaker(self, peer_id: str) -> None:
        if self._guard is not None:
            self._guard.check(f"{_BREAKER_PREFIX}{peer_id}")

    def record_breaker(self, peer_id: str, *, succeeded: bool) -> None:
        if self._guard is not None:
            self._guard.record(f"{_BREAKER_PREFIX}{peer_id}", succeeded=succeeded)

    # ---- 出站 ----
    def _auth_headers(self, peer: PeerConfig) -> dict[str, str]:
        if not peer.auth_secret_name:
            return {}
        provider = self._sdk.registry.try_get("secret.provider")
        if provider is None:
            raise ApiError("AI_CAPABILITY_UNAVAILABLE", "secret.provider is not assembled", 503)
        name = peer.auth_secret_name
        ciphertext = name[len("secret://") :] if name.startswith("secret://") else name
        try:
            plaintext = provider.decrypt(ciphertext)
        except Exception as exc:  # noqa: BLE001 - 解密失败按对端不可用处理，不回显实现细节
            raise ApiError("AI_A2A_PEER_UNAVAILABLE", f"peer {peer.peer_id!r} credential cannot be decrypted", 503) from exc
        if not plaintext:
            raise ApiError("AI_A2A_PEER_UNAVAILABLE", f"peer {peer.peer_id!r} credential is empty", 503)
        return {"Authorization": f"Bearer {plaintext}"}

    async def invoke(
        self,
        peer: PeerConfig,
        payload: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
        identity_headers: dict[str, str] | None = None,
        depth: int | None = None,
    ) -> dict[str, Any]:
        """出站唯一收口：自证权限 → 熔断闸门 → POST → 观测上报 → 返回已校验 body。

        `depth` 是**出站后的新深度**，只用于观测（由网关在扇出时传入）；
        `identity_headers` 与 `extra_headers` 必须分开传：前者由网关依据**已认证**的
        SecurityContext 生成，属于可信身份；后者可能来自调用方透传，必须过白名单。
        早期实现把两者混在一个 dict 里过 `_self_headers`，导致 `X-User-Id` /
        `Authorization` 被当成伪造头过滤掉，`a2a_identity_propagation` 静默失效。
        """
        # 每次出站前自证权限：manifest 未声明 network.outbound 时这里必然抛错
        self._sdk.require_permission("network.outbound")
        started = time.monotonic()
        try:
            self.check_breaker(peer.peer_id)
        except ApiError as exc:
            # 熔断打开是"没发出请求"的失败：必须与对端错误区分上报，否则看板无法分辨
            if exc.code == _CIRCUIT_OPEN_CODE:
                _emit_outbound(peer.peer_id, result=RESULT_CIRCUIT_OPEN, duration_ms=_elapsed_ms(started), response_bytes=0, depth=depth)
            raise
        try:
            body, response_bytes = await self._send(peer, payload, extra_headers=extra_headers, identity_headers=identity_headers)
        except ApiError as exc:
            _emit_outbound(
                peer.peer_id,
                result=RESULT_TIMEOUT if exc.code == _TIMEOUT_CODE else RESULT_ERROR,
                duration_ms=_elapsed_ms(started),
                # 失败路径不做响应体推断：即使收到了 4xx body，也不能把它当作"响应字节数"上报
                response_bytes=0,
                depth=depth,
            )
            raise
        _emit_outbound(peer.peer_id, result=RESULT_OK, duration_ms=_elapsed_ms(started), response_bytes=response_bytes, depth=depth)
        return body

    async def _send(
        self,
        peer: PeerConfig,
        payload: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
        identity_headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], int]:
        """真正发起出站请求；返回 (已解析 body, 响应字节数)。错误映射与熔断计数都在这层。"""
        import httpx

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **_self_headers(extra_headers or {}),
            **(identity_headers or {}),
            **self._auth_headers(peer),
        }
        url = urljoin(peer.base_url + "/", _INVOKE_PATH.lstrip("/"))
        timeout = peer.timeout_seconds or self._config.default_timeout_seconds
        try:
            if self._client is not None:
                response = await self._client.post(url, json=payload, headers=headers, timeout=timeout)
                raw = response.content
                status_code = response.status_code
            else:
                async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
                    async with client.stream("POST", url, json=payload, headers=headers) as response:
                        status_code = response.status_code
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > self._config.max_response_bytes:
                                raise ApiError("AI_A2A_PEER_UNAVAILABLE", f"peer {peer.peer_id!r} response exceeds max_response_bytes", 503)
                            chunks.append(chunk)
                        raw = b"".join(chunks)
        except ApiError:
            self.record_breaker(peer.peer_id, succeeded=False)
            raise
        except httpx.TimeoutException as exc:
            self.record_breaker(peer.peer_id, succeeded=False)
            raise ApiError("AI_A2A_TIMEOUT", f"peer {peer.peer_id!r} timed out after {timeout}s", 504) from exc
        except Exception as exc:  # noqa: BLE001 - 连接级失败统一映射为对端不可用
            self.record_breaker(peer.peer_id, succeeded=False)
            raise ApiError("AI_A2A_PEER_UNAVAILABLE", f"peer {peer.peer_id!r} is unreachable: {exc}", 503) from exc
        if len(raw) > self._config.max_response_bytes:
            self.record_breaker(peer.peer_id, succeeded=False)
            raise ApiError("AI_A2A_PEER_UNAVAILABLE", f"peer {peer.peer_id!r} response exceeds max_response_bytes", 503)
        if status_code >= 500:
            self.record_breaker(peer.peer_id, succeeded=False)
            raise ApiError("AI_A2A_PEER_UNAVAILABLE", f"peer {peer.peer_id!r} returned {status_code}", 503)
        if status_code >= 400:
            self.record_breaker(peer.peer_id, succeeded=False)
            raise ApiError("AI_A2A_DENIED", f"peer {peer.peer_id!r} rejected the call ({status_code})", 403)
        try:
            import json

            body = json.loads(raw or b"{}")
        except ValueError as exc:
            self.record_breaker(peer.peer_id, succeeded=False)
            raise ApiError("AI_A2A_PEER_UNAVAILABLE", f"peer {peer.peer_id!r} returned non-JSON body", 503) from exc
        if not isinstance(body, dict):
            self.record_breaker(peer.peer_id, succeeded=False)
            raise ApiError("AI_A2A_PEER_UNAVAILABLE", f"peer {peer.peer_id!r} returned non-object body", 503)
        self.record_breaker(peer.peer_id, succeeded=True)
        # 对端若按本项目统一响应封装（`{code, message, data, trace_id}`）返回，
        # A2A 响应在 `data` 里；不解包会让出站把成功响应当成"没有 status"的失败。
        # 判定必须严格（顶层有 code 且 data 是带 status 的对象），避免误伤正常的 A2A 响应。
        if "status" not in body and isinstance(body.get("data"), dict) and "status" in body["data"]:
            body = body["data"]
        return body, len(raw)


def _elapsed_ms(started: float) -> int:
    """单调时钟起点的毫秒耗时。"""
    return max(0, int((time.monotonic() - started) * 1000))


def _self_headers(headers: dict[str, str]) -> dict[str, str]:
    """过滤掉调用方可伪造的入站标记头（只允许信令头透传）。"""
    banned = {"x-user-id", "authorization", "host", "content-length"}
    return {key: value for key, value in headers.items() if key.lower() not in banned}
