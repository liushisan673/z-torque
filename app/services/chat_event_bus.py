"""会话消息事件总线：后台链路消息落库后主动推送给控制台会话页。

单进程最小实现：控制台通过 ``GET /api/z-torque/chat/events`` 订阅，
入站 Webhook / 企微等后台链路落库后 publish；多副本部署需要替换为 Redis/DB 广播。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

logger = logging.getLogger("z_torque.chat_event_bus")


@dataclass(frozen=True)
class ChatMessageEvent:
    """一条已落库的会话消息事件；仅用于前端增量刷新。"""

    owner_user_id: UUID
    conversation_id: UUID
    message_id: UUID
    sender_type: str
    content: str
    title: str
    created_at: datetime
    # 消息附件片段（image/file/audio/video + file_id），控制台实时渲染媒体气泡
    content_parts: list[dict] | None = None
    task_id: UUID | None = None
    # 该消息触发的后台任务（用户消息）：控制台据此订阅任务事件流，展示执行过程
    pending_task_id: UUID | None = None


class ChatMessageEventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[ChatMessageEvent]] = set()

    def subscribe(self) -> asyncio.Queue[ChatMessageEvent]:
        queue: asyncio.Queue[ChatMessageEvent] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[ChatMessageEvent]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: ChatMessageEvent) -> None:
        """非阻塞广播：单个订阅者队列满时丢弃该订阅者的这条事件，不影响主链路。"""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("chat message event dropped for slow subscriber conversation_id=%s", event.conversation_id)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


chat_event_bus = ChatMessageEventBus()
