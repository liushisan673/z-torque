from typing import Any
from pydantic import BaseModel


class AuditEventResponse(BaseModel):
    id: str
    action: str
    category: str
    resource_type: str
    resource_id: str | None = None
    actor_user_id: str | None = None
    actor_username: str | None = None
    summary: str
    detail: dict[str, Any] | None = None
    trace_id: str | None = None
    result: str | None = None
    duration_ms: int | None = None
    created_at: str
