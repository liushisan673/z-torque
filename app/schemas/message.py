from datetime import datetime
from uuid import UUID
from pydantic import BaseModel

from app.core.servertime import UTCDatetime

class MessageResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    sender_type: str
    content: str
    content_parts: list[dict] | None
    status: str
    task_id: UUID | None
    error_code: str | None
    created_at: UTCDatetime
    model_config = {"from_attributes": True}