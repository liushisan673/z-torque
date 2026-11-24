"""MCP 生命周期共享服务（builtin plugin 领域对象，不新增内核 Port）。

提供三件事：
1. 进程内共享 ToolRegistry：记录本机已登记的 MCP 工具名与当前 owner；
2. 独占 owner：同一连接器的发现/登记操作互斥，冲突返回明确错误码；
3. 生命周期收口：connect/close 仍由创建连接的一方在 finally 中完成，
   本服务只负责编排与 owner 释放，不把连接泄漏给调用方。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import ApiError

MCP_LIFECYCLE_KEY = "mcp_lifecycle"


@dataclass(frozen=True)
class _OwnerLease:
    owner_id: str
    token: str
    expires_at: float


@dataclass
class McpToolRegistry:
    """单机内存注册表；DB 仍是工具登记的真实来源，这里只做并发门与运行期快照。"""

    owner_ttl_seconds: float = 60.0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _owners: dict[tuple[str, str], _OwnerLease] = field(default_factory=dict)
    _tools: dict[tuple[str, str], set[str]] = field(default_factory=dict)

    def acquire_owner(self, tenant_id: Any, connector_id: Any, owner_id: str) -> str:
        key = (str(tenant_id), str(connector_id))
        now = time.monotonic()
        with self._lock:
            current = self._owners.get(key)
            if current is not None and current.expires_at > now:
                if current.owner_id != owner_id:
                    raise ApiError(
                        "AI_MCP_OWNER_CONFLICT",
                        "MCP connector is owned by another lifecycle operation",
                        409,
                    )
                return current.token
            token = uuid.uuid4().hex
            self._owners[key] = _OwnerLease(owner_id, token, now + self.owner_ttl_seconds)
            return token

    def release_owner(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            for key, lease in list(self._owners.items()):
                if lease.token == token:
                    self._owners.pop(key, None)
                    return

    def record(self, tenant_id: Any, connector_id: Any, names: list[str]) -> None:
        key = (str(tenant_id), str(connector_id))
        with self._lock:
            self._tools.setdefault(key, set()).update(name for name in names if name)

    def forget(self, tenant_id: Any, connector_id: Any, names: list[str] | None = None) -> None:
        key = (str(tenant_id), str(connector_id))
        with self._lock:
            if names is None:
                self._tools.pop(key, None)
                return
            current = self._tools.get(key)
            if current is not None:
                current.difference_update(names)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "owners": len([lease for lease in self._owners.values() if lease.expires_at > time.monotonic()]),
                "tools": sum(len(names) for names in self._tools.values()),
            }

    def clear(self) -> None:
        with self._lock:
            self._owners.clear()
            self._tools.clear()


class McpLifecycleService:
    """共享生命周期服务对象：由 mcp_connector 插件 publish，API 通过 app.state 只读调用。"""

    def __init__(self, gateway: Any = None, registry: McpToolRegistry | None = None) -> None:
        self.gateway = gateway
        self.registry = registry or McpToolRegistry()

    async def discover(self, *, options: dict[str, Any], cursor: str | None = None, max_pages: int = 1) -> dict:
        gateway = self.gateway
        discover = getattr(gateway, "discover_mcp_http", None) if gateway is not None else None
        if not callable(discover):
            raise ApiError("AI_MCP_LIFECYCLE_UNAVAILABLE", "MCP lifecycle service is unavailable", 503)
        return await discover(**options, cursor=cursor, max_pages=max_pages)

    def status(self) -> dict[str, Any]:
        return {"available": callable(getattr(self.gateway, "discover_mcp_http", None)), **self.registry.snapshot()}

    def aclose(self) -> None:
        self.registry.clear()
