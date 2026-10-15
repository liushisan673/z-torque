from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.responses import success_response
from app.db.session import get_db
from app.kernel.model_gateway import ModelRequest
from app.models import Conversation, Message, User
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_application import handle_chat
from app.services.context_builder import build_context
from app.services.stream_service import stream_text

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("")
async def chat(payload: ChatRequest, request: Request, idempotency_key: str | None = Header(None, alias="Idempotency-Key"), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    result = await handle_chat(db, tenant_id=user.tenant_id, user_id=user.id, agent_id=payload.agent_id, conversation_id=payload.conversation_id, content=payload.message, content_parts=payload.content_parts, idempotency_key=idempotency_key, trace_id=request.state.trace_id, model_gateway=getattr(request.app.state, "model_gateway", None), skill_gateway=getattr(request.app.state, "skill_gateway", None))
    db.commit()
    waiting = result.pending_confirmation is not None
    response = ChatResponse(
        response_type="CHAT" if result.assistant_message else "TASK",
        conversation_id=result.user_message.conversation_id,
        user_message_id=result.user_message.id,
        assistant_message_id=result.assistant_message.id if result.assistant_message else None,
        task_id=result.task_id,
        status="WAITING_HUMAN" if waiting else ("COMPLETED" if result.assistant_message else "QUEUED"),
        confirmation=result.pending_confirmation,
    )
    return success_response(request, response.model_dump(mode="json"))


@router.post("/stream")
async def stream_chat(payload: ChatRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if payload.stream is not True:
        raise ValueError("stream must be true")
    conversation = db.get(Conversation, payload.conversation_id) if payload.conversation_id else None
    if conversation is None or conversation.tenant_id != user.tenant_id or conversation.owner_user_id != user.id:
        raise ValueError("conversation is required for streaming chat")
    user_message = Message(tenant_id=user.tenant_id, conversation_id=conversation.id, sender_type="USER", content=payload.message, content_parts=payload.content_parts, status="COMPLETED")
    db.add(user_message)
    db.flush()
    gateway = getattr(request.app.state, "model_gateway", None)
    if gateway is None:
        raise ValueError("model gateway unavailable")
    model_request = ModelRequest(
        trace_id=request.state.trace_id,
        conversation_id=conversation.id,
        messages=build_context(db, conversation),
        model=getattr(gateway, "default_model", "mock"),
        stream=True,
    )
    db.commit()
    return StreamingResponse(stream_text(gateway, model_request), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Trace-ID": request.state.trace_id})
