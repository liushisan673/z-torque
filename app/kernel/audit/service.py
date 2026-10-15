from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import AuditLog


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
