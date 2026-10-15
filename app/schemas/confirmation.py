from uuid import UUID
from pydantic import BaseModel


class ConfirmationResolveRequest(BaseModel):
    approved: bool


class ConfirmationResponse(BaseModel):
    id: UUID
    status: str
    requested_by_user_id: UUID
    confirmed_by_user_id: UUID | None
    reason: str | None
    task_id: UUID | None
    skill_version_id: UUID | None
    tool_version_id: UUID | None
    resolved_at: object | None

    model_config = {"from_attributes": True}
