from uuid import UUID

from pydantic import BaseModel, Field

from app.core.servertime import UTCDatetime


class ConversationCreate(BaseModel):
    agent_id: UUID | None = None
    title: str | None = Field(default=None, max_length=256)

class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=256)

class ConversationResponse(BaseModel):
    id: UUID
    agent_id: UUID
    title: str | None
    status: str
    last_message_at: UTCDatetime | None
    model_config = {"from_attributes": True}