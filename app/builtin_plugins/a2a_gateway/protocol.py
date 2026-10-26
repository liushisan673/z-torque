"""A2A 协议适配层（v1.3 §6.2）。

固定 v1 线格式（外部对端契约，改动需版本化）：

请求
```json
{"a2a_version": "1.0", "id": "<uuid>", "method": "a2a.invoke",
 "params": {"route": "local", "skill_version_id": "...", "arguments": {}},
 "depth": 0}
```
- `route=local`  → 交给 `skill.executor`（SkillGateway）；
- `route=remote` → `params.peer` 指定白名单内对端，`params.skill` 指定对端技能名。

响应
```json
{"a2a_version": "1.0", "id": "...", "status": "SUCCEEDED|FAILED|DENIED",
 "data": {...}, "error": {"code": "...", "message": "..."}, "peer": "...", "depth": 0}
```

深度头：`X-A2A-Depth`（缺省 0，非整数 → `AI_A2A_DENIED`）；出站写 `depth+1`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID, uuid4

from app.core.errors import ApiError

A2A_VERSION = "1.0"
A2A_DEPTH_HEADER = "X-A2A-Depth"
A2A_TRACE_HEADER = "X-A2A-Trace"
A2A_ROUTE_HEADER = "X-A2A-Route"  # 出站标记：对端据此判断"这是 A2A 入站"而非普通 API

ROUTE_LOCAL = "local"
ROUTE_REMOTE = "remote"
ROUTES = (ROUTE_LOCAL, ROUTE_REMOTE)

STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
STATUS_DENIED = "DENIED"


@dataclass(frozen=True)
class A2ARequest:
    """标准化后的入站请求（payload 已校验，不再透传原始结构给执行层）。"""

    request_id: str
    route: str
    depth: int
    skill_version_id: UUID | None = None
    peer: str | None = None
    skill: str | None = None
    arguments: dict[str, Any] | None = None

    def outbound_payload(self) -> dict[str, Any]:
        """出站载荷：只保留路由必要字段，不带调用方身份凭据。

        注意这是**发送方**视角的载荷（`route` 原样保留）；真正的出站由
        `A2AGateway._invoke_remote` 决定语义：调用方提供了 `skill_version_id` 时
        会把 `route` 改写成 `local`，让对端直接执行而不是再路由一次。
        """
        params: dict[str, Any] = {"route": self.route, "arguments": self.arguments or {}}
        if self.skill_version_id is not None:
            params["skill_version_id"] = str(self.skill_version_id)
        if self.peer:
            params["peer"] = self.peer
        if self.skill:
            params["skill"] = self.skill
        return {"a2a_version": A2A_VERSION, "id": self.request_id, "method": "a2a.invoke", "params": params, "depth": self.depth}


def parse_depth(headers: Mapping[str, str], *, max_depth: int) -> int:
    """解析 `X-A2A-Depth`；非整数或负数直接拒绝（不静默归零）。"""
    raw = headers.get(A2A_DEPTH_HEADER) or headers.get(A2A_DEPTH_HEADER.lower())
    if raw in (None, ""):
        return 0
    try:
        depth = int(str(raw).strip())
    except ValueError as exc:
        raise ApiError("AI_A2A_DENIED", "invalid X-A2A-Depth header", 403) from exc
    if depth < 0:
        raise ApiError("AI_A2A_DENIED", "invalid X-A2A-Depth header", 403)
    if depth > max_depth:
        raise ApiError("AI_A2A_DEPTH_EXCEEDED", f"A2A depth {depth} exceeds max_depth {max_depth}", 400)
    return depth


def parse_request(payload: Any) -> A2ARequest:
    """校验入站信封；任何结构问题都是 `AI_A2A_DENIED`（不 500）。"""
    if not isinstance(payload, dict):
        raise ApiError("AI_A2A_DENIED", "A2A payload must be a JSON object", 403)
    if str(payload.get("a2a_version") or A2A_VERSION) != A2A_VERSION:
        raise ApiError("AI_A2A_DENIED", f"unsupported a2a_version: {payload.get('a2a_version')}", 403)
    params = payload.get("params")
    if not isinstance(params, dict):
        raise ApiError("AI_A2A_DENIED", "A2A params must be a JSON object", 403)
    route = str(params.get("route") or ROUTE_LOCAL).strip().lower()
    if route not in ROUTES:
        raise ApiError("AI_A2A_ROUTE_FAILED", f"unsupported route: {route}", 404)
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise ApiError("AI_A2A_DENIED", "A2A params.arguments must be a JSON object", 403)
    try:
        depth = int(payload.get("depth") or 0)
    except (TypeError, ValueError) as exc:
        raise ApiError("AI_A2A_DENIED", "A2A depth must be an integer", 403) from exc
    skill_version_id: UUID | None = None
    if params.get("skill_version_id"):
        try:
            skill_version_id = UUID(str(params["skill_version_id"]))
        except ValueError as exc:
            raise ApiError("AI_A2A_ROUTE_FAILED", "invalid skill_version_id", 404) from exc
    peer = str(params["peer"]).strip() if params.get("peer") else None
    skill = str(params["skill"]).strip() if params.get("skill") else None
    if route == ROUTE_LOCAL and skill_version_id is None:
        raise ApiError("AI_A2A_ROUTE_FAILED", "local route requires params.skill_version_id", 404)
    if route == ROUTE_REMOTE and not (peer and skill):
        raise ApiError("AI_A2A_ROUTE_FAILED", "remote route requires params.peer and params.skill", 404)
    return A2ARequest(
        request_id=str(payload.get("id") or uuid4()),
        route=route,
        depth=depth,
        skill_version_id=skill_version_id,
        peer=peer,
        skill=skill,
        arguments=arguments,
    )


def build_response(
    *,
    request_id: str,
    status: str,
    data: Any = None,
    error_code: str | None = None,
    error_message: str | None = None,
    peer: str | None = None,
    depth: int = 0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "a2a_version": A2A_VERSION,
        "id": request_id,
        "status": status,
        "peer": peer,
        "depth": depth,
    }
    if data is not None:
        payload["data"] = data
    if error_code:
        payload["error"] = {"code": error_code, "message": error_message or error_code}
    return payload


def response_from_result(*, request_id: str, result: Any, peer: str | None = None, depth: int = 0) -> dict[str, Any]:
    """ExecutionResult → A2A 响应（status/data/error 三段固定）。"""
    status = str(getattr(result, "status", STATUS_FAILED))
    if status == STATUS_SUCCEEDED:
        return build_response(request_id=request_id, status=STATUS_SUCCEEDED, data=getattr(result, "data", None), peer=peer, depth=depth)
    code = getattr(result, "error_code", None) or "AI_A2A_ROUTE_FAILED"
    return build_response(
        request_id=request_id,
        status=STATUS_DENIED if code in ("AI_PERMISSION_DENIED", "AI_A2A_DENIED") else STATUS_FAILED,
        error_code=code,
        error_message=getattr(result, "error_message", None),
        peer=peer,
        depth=depth,
    )
