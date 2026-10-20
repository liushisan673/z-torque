from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.kernel.ports.interceptor import ExecutionInterceptor
from app.models import AuditLog


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
        trace_id=trace_id,
    )
    db.add(entry)
    db.flush()
    return entry
