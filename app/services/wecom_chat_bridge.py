"""把企微入站消息桥接到本地 Conversation/Message，供控制台 Chat 页展示。

最小实现约束：
- 复用现有 ``Conversation`` / ``Message``，不新增表、不改前端数据模型；
- 会话与消息 ID 用 ``uuid5`` 从 ``conversation_key`` / ``event_id`` 派生，天然幂等；
- owner 由 ``WECOM_CHAT_OWNER_USERNAME`` 指定（默认 ``admin``），登录该账号即可在
  Chat 页看到「企微 · xxx」会话；
- agent 由 ``WECOM_CHAT_AGENT_ID`` 指定；未指定时取租户内第一个已发布 Agent。

本类只负责落库，不负责调用能力服务；回复链路仍在 ``WeComEventService``。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Agent, Conversation, Message, User
from app.services.chat_event_bus import ChatMessageEvent, chat_event_bus

if TYPE_CHECKING:
    from app.services.wecom_events import WeComEvent

logger = logging.getLogger("z_torque.wecom_chat")

_TITLE_MAX_LEN = 200


@dataclass(frozen=True)
class ChatWriteResult:
    """落库结果：供桥接层发布 SSE 事件，并返回 conversation_id 给回复链路。"""

    owner_user_id: UUID
    conversation_id: UUID
    message_id: UUID
    sender_type: str
    content: str
    title: str
    created_at: datetime
    created: bool
    content_parts: list[dict] | None = None
    task_id: UUID | None = None


@dataclass(frozen=True)
class ConversationRef:
    """企微外部会话对应的本地会话（不包含具体消息）。"""

    tenant_id: UUID
    owner_user_id: UUID
    conversation_id: UUID
    agent_id: UUID
    title: str


class WeComChatBridge:
    """企微事件 → 控制台 Chat 会话/消息的写入器。"""

    def __init__(self, settings: Settings | None = None, *, session_factory: Callable[[], Session] | None = None) -> None:
        self.settings = settings or get_settings()
        self._session_factory = session_factory

    @property
    def enabled(self) -> bool:
        return bool(self.settings.wecom_chat_enabled)

    async def record_inbound(self, event: "WeComEvent") -> UUID | None:
        """写入一条 USER 消息，返回本地 conversation_id；跳过/失败返回 None。"""
        if not self.enabled or not event.text:
            return None
        result = await asyncio.to_thread(self._record_inbound_sync, event)
        if result is None:
            return None
        if result.created:
            self._publish(result)
        return result.conversation_id

    async def ensure_conversation(self, event: "WeComEvent") -> ConversationRef | None:
        """只保证本地会话存在，不写消息；供 handle_chat 自己写用户/助手消息。"""
        if not self.enabled:
            return None
        return await asyncio.to_thread(self._ensure_conversation_sync, event)

    def publish_message(self, ref: ConversationRef, message: Message) -> None:
        """把 handle_chat / TaskRuntime 已落库的消息推给前端（不重复写库）。"""
        self._publish(
            ChatWriteResult(
                owner_user_id=ref.owner_user_id,
                conversation_id=ref.conversation_id,
                message_id=message.id,
                sender_type=message.sender_type,
                content=message.content,
                content_parts=message.content_parts,
                title=ref.title,
                created_at=message.created_at or datetime.now(timezone.utc),
                created=True,
                task_id=message.task_id,
            )
        )

    async def record_reply(self, conversation_id: UUID | None, event: "WeComEvent", text: str) -> None:
        """回复成功后写入一条 ASSISTANT 消息；失败只记日志，不影响主链路。"""
        if not self.enabled or conversation_id is None or not text:
            return
        result = await asyncio.to_thread(self._record_reply_sync, conversation_id, event.event_id, text)
        if result is not None and result.created:
            self._publish(result)

    @staticmethod
    def _publish(result: ChatWriteResult) -> None:
        chat_event_bus.publish(
            ChatMessageEvent(
                owner_user_id=result.owner_user_id,
                conversation_id=result.conversation_id,
                message_id=result.message_id,
                sender_type=result.sender_type,
                content=result.content,
                content_parts=result.content_parts,
                title=result.title,
                created_at=result.created_at,
                task_id=result.task_id,
            )
        )

    def _ensure_conversation_sync(self, event: "WeComEvent") -> ConversationRef | None:
        try:
            with self._session() as db:
                owner = self._resolve_owner(db)
                if owner is None:
                    return None
                agent = self._resolve_agent(db, owner, event)
                if agent is None:
                    return None
                key = self._conversation_lookup_key(event)
                conversation_id = uuid5(NAMESPACE_URL, f"z-torque:wecom:conversation:{owner.tenant_id}:{key}")
                conversation = db.get(Conversation, conversation_id)
                now = datetime.now(timezone.utc)
                if conversation is None:
                    conversation = Conversation(
                        id=conversation_id,
                        tenant_id=owner.tenant_id,
                        owner_user_id=owner.id,
                        agent_id=agent.id,
                        title=self._title(event),
                        status="ACTIVE",
                        last_message_at=now,
                    )
                    db.add(conversation)
                    db.flush()
                else:
                    if conversation.tenant_id != owner.tenant_id or conversation.owner_user_id != owner.id:
                        logger.error(
                            "WECOM_ALERT chat bridge skipped: conversation owner mismatch conversation_id=%s event_id=%s",
                            conversation_id,
                            event.event_id,
                        )
                        return None
                    if conversation.status == "ARCHIVED":
                        conversation.status = "ACTIVE"
                        conversation.archived_at = None
                    conversation.last_message_at = now
                title = conversation.title or self._title(event)
                db.commit()
                return ConversationRef(
                    tenant_id=owner.tenant_id,
                    owner_user_id=owner.id,
                    conversation_id=conversation.id,
                    agent_id=conversation.agent_id,
                    title=title,
                )
        except Exception:
            logger.exception("WECOM_ALERT chat bridge failed to ensure conversation event_id=%s event_type=%s", event.event_id, event.event_type)
            return None

    def _record_inbound_sync(self, event: "WeComEvent") -> ChatWriteResult | None:
        try:
            with self._session() as db:
                owner = self._resolve_owner(db)
                if owner is None:
                    return None
                agent = self._resolve_agent(db, owner, event)
                if agent is None:
                    return None
                key = self._conversation_lookup_key(event)
                conversation_id = uuid5(NAMESPACE_URL, f"z-torque:wecom:conversation:{owner.tenant_id}:{key}")
                conversation = db.get(Conversation, conversation_id)
                now = datetime.now(timezone.utc)
                if conversation is None:
                    conversation = Conversation(
                        id=conversation_id,
                        tenant_id=owner.tenant_id,
                        owner_user_id=owner.id,
                        agent_id=agent.id,
                        title=self._title(event),
                        status="ACTIVE",
                        last_message_at=now,
                    )
                    db.add(conversation)
                    db.flush()
                else:
                    if conversation.tenant_id != owner.tenant_id or conversation.owner_user_id != owner.id:
                        logger.error(
                            "WECOM_ALERT chat bridge skipped: conversation owner mismatch conversation_id=%s event_id=%s",
                            conversation_id,
                            event.event_id,
                        )
                        return None
                    if conversation.status == "ARCHIVED":
                        conversation.status = "ACTIVE"
                        conversation.archived_at = None
                    conversation.last_message_at = now
                message_id = uuid5(NAMESPACE_URL, f"z-torque:wecom:message:{event.event_id}")
                created = db.get(Message, message_id) is None
                if created:
                    db.add(
                        Message(
                            id=message_id,
                            tenant_id=owner.tenant_id,
                            conversation_id=conversation.id,
                            sender_type="USER",
                            content=event.text,
                            content_parts=None,
                            status="COMPLETED",
                        )
                    )
                title = conversation.title or self._title(event)
                db.commit()
                return ChatWriteResult(
                    owner_user_id=owner.id,
                    conversation_id=conversation.id,
                    message_id=message_id,
                    sender_type="USER",
                    content=event.text or "",
                    title=title,
                    created_at=now,
                    created=created,
                )
        except Exception:
            logger.exception("WECOM_ALERT chat bridge failed to persist inbound event_id=%s event_type=%s", event.event_id, event.event_type)
            return None

    def _record_reply_sync(self, conversation_id: UUID, event_id: str, text: str) -> ChatWriteResult | None:
        try:
            with self._session() as db:
                conversation = db.get(Conversation, conversation_id)
                if conversation is None:
                    logger.warning("WECOM_ALERT chat bridge reply skipped: conversation missing conversation_id=%s event_id=%s", conversation_id, event_id)
                    return None
                message_id = uuid5(NAMESPACE_URL, f"z-torque:wecom:reply:{event_id}")
                created = db.get(Message, message_id) is None
                now = datetime.now(timezone.utc)
                if created:
                    db.add(
                        Message(
                            id=message_id,
                            tenant_id=conversation.tenant_id,
                            conversation_id=conversation.id,
                            sender_type="ASSISTANT",
                            content=text,
                            content_parts=None,
                            status="COMPLETED",
                        )
                    )
                    conversation.last_message_at = now
                title = conversation.title or "企微会话"
                db.commit()
                return ChatWriteResult(
                    owner_user_id=conversation.owner_user_id,
                    conversation_id=conversation.id,
                    message_id=message_id,
                    sender_type="ASSISTANT",
                    content=text,
                    title=title,
                    created_at=now,
                    created=created,
                )
        except Exception:
            logger.exception("WECOM_ALERT chat bridge failed to persist reply event_id=%s", event_id)
            return None

    def _session(self) -> Session:
        if self._session_factory is not None:
            return self._session_factory()
        # 延迟导入：测试 conftest 先设置 DATABASE_URL，再 import app.db.session
        from app.db.session import SessionLocal

        return SessionLocal()

    def _resolve_owner(self, db: Session) -> User | None:
        username = (self.settings.wecom_chat_owner_username or "").strip()
        if not username:
            logger.error("WECOM_ALERT chat bridge skipped: WECOM_CHAT_OWNER_USERNAME is empty")
            return None
        owner = db.scalar(select(User).where(User.username == username, User.is_active.is_(True)))
        if owner is None:
            logger.error("WECOM_ALERT chat bridge skipped: owner not found username=%s", username)
        return owner

    def _resolve_agent(self, db: Session, owner: User, event: "WeComEvent") -> Agent | None:
        configured = self._specific_agent_id(event) or (self.settings.wecom_chat_agent_id or "").strip()
        if configured:
            try:
                agent_id = UUID(configured)
            except ValueError:
                logger.warning("WECOM_ALERT chat bridge: configured agent is not a UUID, fallback to published agent")
            else:
                agent = db.get(Agent, agent_id)
                if agent is not None and agent.tenant_id == owner.tenant_id and agent.status == "ACTIVE" and agent.current_published_version_id is not None:
                    return agent
                logger.warning("WECOM_ALERT chat bridge: configured agent unavailable, fallback to published agent agent_id=%s", agent_id)
        agent = db.scalar(
            select(Agent)
            .where(
                Agent.tenant_id == owner.tenant_id,
                Agent.status == "ACTIVE",
                Agent.current_published_version_id.is_not(None),
            )
            .order_by(Agent.is_default_parent.desc(), Agent.created_at)
            .limit(1)
        )
        if agent is None:
            logger.error("WECOM_ALERT chat bridge skipped: no published agent tenant_id=%s", owner.tenant_id)
        return agent

    def _specific_agent_id(self, event: "WeComEvent") -> str:
        """按事件通道选择专用 Agent；未配置时返回空串，回落到 WECOM_CHAT_AGENT_ID。"""
        channel = self._channel_label(event)
        if channel == "bot":
            return (self.settings.wecom_agent_id_bot or "").strip()
        if channel == "kf":
            return (self.settings.wecom_agent_id_kf or "").strip()
        return (self.settings.wecom_agent_id_app or "").strip()

    @staticmethod
    def _channel_label(event: "WeComEvent") -> str:
        channel = (event.channel or "").lower()
        event_type = (event.event_type or "").lower()
        if channel == "wecom_bot" or event_type.startswith("wecom.bot."):
            return "bot"
        if channel == "wecom_kf" or event_type.startswith("wecom.kf."):
            return "kf"
        return "app"

    def _conversation_lookup_key(self, event: "WeComEvent") -> str:
        """按通道拆分 Agent 时把通道加入本地会话键，避免应用/机器人共用同一会话。"""
        key = event.conversation_key or event.external_chat_id or event.external_user_id or event.event_id
        if self._specific_agent_id(event):
            return f"{self._channel_label(event)}:{key}"
        return key

    @staticmethod
    def _title(event: "WeComEvent") -> str:
        key = event.external_user_id or event.external_chat_id or event.conversation_key or event.event_id
        label = {"bot": "企微机器人", "kf": "企微客服"}.get(WeComChatBridge._channel_label(event), "企微应用")
        return f"{label} · {key}"[:_TITLE_MAX_LEN]
