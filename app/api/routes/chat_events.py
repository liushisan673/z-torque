"""控制台会话消息事件流（SSE）。

入站 Webhook、企微等后台链路把消息写入 ``Conversation`` / ``Message`` 后，
通过进程内总线立即推给控制台；前端不再轮询。多副本需替换为 Redis/DB 广播。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user
from app.models import User
from app.services.chat_event_bus import chat_event_bus

router = APIRouter(prefix="/chat", tags=["chat"])

logger = logging.getLogger("z_torque.chat_events_stream")

_KEEPALIVE_SECONDS = 15.0


@router.get("/events", summary="控制台会话消息事件流（SSE）")
async def stream(request: Request, user: User = Depends(get_current_user)) -> StreamingResponse:
    queue = chat_event_bus.subscribe()

    async def events() -> AsyncIterator[str]:
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                # 只推给会话 owner，避免其他登录用户看到不属于自己的后台消息
                if event.owner_user_id != user.id:
                    continue
                payload = {
                    "conversation_id": str(event.conversation_id),
                    "message_id": str(event.message_id),
                    "sender_type": event.sender_type,
                    "content": event.content,
                    "content_parts": event.content_parts,
                    "title": event.title,
                    "created_at": event.created_at.isoformat(),
                    "task_id": str(event.task_id) if event.task_id is not None else None,
                    "pending_task_id": str(event.pending_task_id) if event.pending_task_id is not None else None,
                }
                yield f"id: {event.message_id}\nevent: chat.message\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            chat_event_bus.unsubscribe(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
