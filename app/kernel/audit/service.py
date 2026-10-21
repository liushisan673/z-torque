import contextvars
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.kernel.ports.interceptor import ExecutionInterceptor
from app.models import AuditLog

logger = logging.getLogger("z_torque.audit")

# 请求级 trace 兜底：中间件注入当前请求的 trace_id，record_audit 未显式传 trace 时
# 自动回填（覆盖服务层内部审计点），使审计页 Trace ID 列与 /audit/by-trace 可用。
# 显式传入的 trace_id 优先；后台 worker（无请求上下文）不受影响。
_request_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("zt_request_trace_id", default=None)


def set_request_trace_id(trace_id: str | None) -> None:
    _request_trace_id.set(trace_id)


class AuditExecutionInterceptor(ExecutionInterceptor):
    """interceptor Port 内核默认实现：审计事件发射（委托 record_audit 写入路径）。

    审计事件流写入属内核不变量4（不可关闭），本实现保证拦截链默认行为与
    直调 record_audit 完全一致；企业版可在实现内叠加配额 / 熔断等策略
    （契约 §5.5），或替换为独立审计管道。
    """

    def on_execution(self, db: Session, *, tenant_id: UUID, actor_user_id: UUID | None, action: str, resource_type: str, resource_id: UUID | None, summary: str, detail: dict[str, Any] | None = None, trace_id: str | None = None) -> None:
        record_audit(db, tenant_id=tenant_id, actor_user_id=actor_user_id, action=action, resource_type=resource_type, resource_id=resource_id, summary=summary, detail=detail, trace_id=trace_id)


def record_audit(
    db: Session,
    *,
    tenant_id: UUID,
    action: str,
    resource_type: str,
    resource_id: UUID | None,
    actor_user_id: UUID | None,
    summary: str,
    detail: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        tenant_id=tenant_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        actor_user_id=actor_user_id,
        summary=summary,
        detail=detail,
        trace_id=trace_id or _request_trace_id.get(),
    )
    db.add(entry)
    db.flush()
    return entry


def record_audit_detached(
    *,
    tenant_id: UUID,
    action: str,
    resource_type: str,
    resource_id: UUID | None,
    actor_user_id: UUID | None,
    summary: str,
    detail: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> AuditLog | None:
    """在独立事务中写入审计事件，用于「拒绝/异常」路径（P2-12）。

    这类事件抛出 ApiError 后调用方事务会回滚，若与业务同事务写入则必然丢失；
    而审计事件流不可关闭（契约不变量4），拒绝行为必须留痕。写入失败只记日志，
    不得阻断原本的拒绝响应。
    """
    from app.db.session import SessionLocal

    try:
        with SessionLocal.begin() as db:
            return record_audit(
                db,
                tenant_id=tenant_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                actor_user_id=actor_user_id,
                summary=summary,
                detail=detail,
                trace_id=trace_id,
            )
    except Exception:  # noqa: BLE001 - 审计写入失败不得影响拒绝语义
        logger.exception("failed to persist detached audit event: %s", action)
        return None
