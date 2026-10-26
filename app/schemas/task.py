from uuid import UUID

from pydantic import BaseModel, Field

from app.core.servertime import UTCDatetime
from app.kernel.task.states import TaskStatus


class TaskCreateRequest(BaseModel):
    agent_id: UUID
    agent_version_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=128)
    request_summary: str = Field(min_length=1, max_length=4000)
    parent_task_id: UUID | None = None
    delegation_id: UUID | None = None
    child_sequence: int | None = Field(default=None, ge=1)
    depends_on_task_ids: list[str] | None = None
    deadline_at: UTCDatetime | None = None
    conversation_id: UUID | None = None
    trigger_message_id: UUID | None = None
    message_id: UUID | None = None

    def resolved_trigger_message_id(self) -> UUID | None:
        if self.trigger_message_id and self.message_id and self.trigger_message_id != self.message_id:
            raise ValueError("trigger_message_id and legacy message_id must match")
        return self.trigger_message_id or self.message_id


class TaskResponse(BaseModel):
    id: UUID
    status: TaskStatus
    owner_user_id: UUID
    root_task_id: UUID
    delegation_depth: int
    request_summary: str
    result: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: UTCDatetime
    updated_at: UTCDatetime
    conversation_id: UUID | None = None
    trigger_message_id: UUID | None = None
    message_id: UUID | None = None

    model_config = {"from_attributes": True}


class TaskEventResponse(BaseModel):
    seq: int
    event_type: str
    from_state: str | None
    to_state: str | None
    summary: str
    payload_summary: dict | None
    trace_id: str | None
    created_at: UTCDatetime

    model_config = {"from_attributes": True}
