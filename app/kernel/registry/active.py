"""运行期 Port 访问器（P2-14）。

装配根在 `registry.resolve()` 后调用 `bind_registry()`；没有 Request 上下文的内核/服务
代码（任务状态机、上下文组装、确认流）借此取到"经解析的 Port"，让 memory.working /
memory.long / notify.channel / observability.sink / policy.source 成为真实调用缝。

未绑定（单元测试直构）时一律降级为"无 Port"，不改变既有行为。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.errors import ApiError

logger = logging.getLogger("z_torque.ports")

_registry: Any | None = None


def bind_registry(registry: Any) -> None:
    global _registry
    _registry = registry


def unbind_registry() -> None:
    global _registry
    _registry = None


def active_registry() -> Any | None:
    return _registry


def try_port(name: str) -> Any | None:
    """取已解析 Port；未绑定或 UNAVAILABLE 返回 None（用于可选增强，不抛错）。"""
    registry = _registry
    if registry is None:
        return None
    try:
        return registry.try_get(name)
    except Exception:  # pragma: no cover - 防御装配期异常，调用点不应因此失败
        logger.exception("failed to resolve port %s", name)
        return None


def call_optional(
    db: Any,
    *,
    audit_tenant_id: Any,
    port_name: str,
    method: str,
    actor_user_id: Any = None,
    trace_id: str | None = None,
    **kwargs: Any,
) -> Any | None:
    """调用可选 Port；能力不可用时落审计并降级返回 None（阶段00 §3 可选缺失语义）。

    其余 ApiError 原样抛出，避免把真实故障吞掉。
    """
    registry = _registry
    if registry is None:
        return None
    try:
        port = registry.get(port_name)
        return getattr(port, method)(**kwargs)
    except ApiError as exc:
        if exc.code != "AI_CAPABILITY_UNAVAILABLE":
            raise
        logger.warning("port %s is unavailable; %s() skipped", port_name, method)
        _audit_unavailable(
            db,
            tenant_id=audit_tenant_id,
            actor_user_id=actor_user_id,
            port_name=port_name,
            method=method,
            trace_id=trace_id,
        )
        return None


def _audit_unavailable(
    db: Any,
    *,
    tenant_id: Any,
    actor_user_id: Any,
    port_name: str,
    method: str,
    trace_id: str | None,
) -> None:
    if db is None or tenant_id is None:
        return
    from app.kernel.audit.service import record_audit

    try:
        record_audit(
            db,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="CAPABILITY_UNAVAILABLE",
            resource_type="PORT",
            resource_id=None,
            summary=f"capability {port_name} is unavailable",
            detail={"port": port_name, "call": method},
            trace_id=trace_id,
        )
    except Exception:  # pragma: no cover - 审计失败不得影响主流程
        logger.exception("failed to audit unavailable capability %s", port_name)


def emit_event(event: str, payload: dict[str, Any] | None = None) -> None:
    """向 observability.sink 发事件；sink 故障不影响主流程。"""
    sink = try_port("observability.sink")
    if sink is None:
        return
    try:
        sink.emit(event, payload or {})
    except Exception:  # pragma: no cover
        logger.warning("observability sink failed for event %s", event, exc_info=True)
