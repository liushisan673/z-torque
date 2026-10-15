from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    agent_id: UUID | None = None
    conversation_id: UUID | None = None
    message: str = Field(min_length=1, max_length=20_000)
    content_parts: list[dict[str, Any]] | None = None
    stream: bool = False


class ChatResponse(BaseModel):
    response_type: str
    conversation_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID | None = None
    task_id: UUID | None = None
    status: str
    # status=WAITING_HUMAN 时的审批挂起信息（confirmation_id/tool_name/arguments/task_id）
    confirmation: dict | None = None
