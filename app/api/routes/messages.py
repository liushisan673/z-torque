from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response
from app.db.session import get_db
from app.models import Conversation, Message, User
from app.schemas.message import MessageResponse

router = APIRouter(prefix="/messages", tags=["messages"])


@router.get("/{message_id}")
async def detail(message_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    message = db.scalar(
        select(Message).join(Conversation, Conversation.id == Message.conversation_id).where(
            Message.id == message_id,
            Message.tenant_id == user.tenant_id,
            Conversation.owner_user_id == user.id,
        )
    )
    if message is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "message not found", 404)
    return success_response(request, MessageResponse.model_validate(message).model_dump(mode="json"))