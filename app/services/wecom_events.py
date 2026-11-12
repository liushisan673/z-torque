"""企微能力服务事件订阅（AI 中台作为接收方）。

能力服务把企业微信入站事件按订阅推送到本服务的 HTTPS Webhook；本模块只做四件事：

1. HMAC-SHA256 验签：``sha256=HMAC(secret, f"{timestamp}.{rawBody}")``，
   原始 body 必须参与计算，不能先用 JSON body parser 改写；
2. 事件去重（``x-wecom-event-id``，TTL 默认 24h）；
3. 验签通过后立即 ACK，再异步处理（回调必须 5 秒内返回）；
4. 经能力服务 ``/v1/reply`` 或 ``/v1/messages`` 回复。

约束：不直连企业微信 API；不自动创建订阅（订阅由能力服务端创建）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select

from app.config import Settings, get_settings
from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.kernel.security_context import SecurityContext
from app.kernel.task.states import TERMINAL_STATES
from app.models import Message, Task, TaskEvent
from app.services.chat_application import handle_chat
from app.services.wecom_chat_bridge import ConversationRef, WeComChatBridge

logger = logging.getLogger("z_torque.wecom_events")

# 这些工具会直接把结果发回“当前会话”；命中且成功时 Webhook 不再补发 /v1/reply。
# 注意：wecom_send_to_user / wecom_send_to_chat / wecom_message_push 是发给“别人”的动作，
# 不应抑制当前会话的最终回复。
_WECOM_REPLY_TOOLS = frozenset(
    {
        "wecom_reply",
        "wecom_message_send",
        "wecom_message_send_card",
        "wecom_message_send_media",
        "wecom_bot_send",
        "wecom_bot_send_media",
        "wecom_kf_send",
        "wecom_kf_send_media",
    }
)

# 能力服务当前支持的事件类型；未知类型只 ACK 不处理，避免因新增类型导致重试风暴
SUPPORTED_EVENT_TYPES = frozenset(
    {
        "wecom.message.received",
        "wecom.bot.message.received",
        "wecom.kf.message.received",
        "wecom.bot.template_card.event",
    }
)

# 事件字段可能平铺、也可能包在 event / data 里；用于选择真正的 body
_EVENT_KEYS = frozenset(
    {
        "event_id",
        "event_type",
        "conversation_key",
        "external_user_id",
        "external_chat_id",
        "text",
        "content",
    }
)


def _plain_secret(value: Any) -> str:
    """SecretStr / str / None → 明文（仅用于请求头，禁止写日志）。"""
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if callable(getter) else value)


def compute_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """按能力服务约定计算签名：``sha256=`` + HMAC-SHA256(secret, timestamp.rawBody)。"""
    message = timestamp.encode("utf-8") + b"." + raw_body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(*, secret: str, timestamp: str, raw_body: bytes, signature: str) -> bool:
    """timing-safe 比较签名；缺少任一参数直接失败。"""
    if not secret or not timestamp or not signature:
        return False
    expected = compute_signature(secret, timestamp, raw_body)
    # hex 大小写不改变语义；先统一小写再恒定时间比较
    return hmac.compare_digest(signature.strip().lower(), expected.lower())


def _as_optional_str(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _extract_text(value: Any) -> str | None:
    """text 可能是字符串，也可能是 {content: ...} / {text: ...}。"""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("content", "text"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


def _extract_text_from_body(body: dict[str, Any]) -> str | None:
    """兼容 text / content / message / payload / body / data 以及 mixed 子项。"""
    direct = _extract_text(body.get("text")) or _extract_text(body.get("content"))
    if direct:
        return direct
    mixed = body.get("mixed")
    if isinstance(mixed, dict):
        items = mixed.get("msg_item")
        if isinstance(items, list):
            parts = [_extract_text(item.get("text")) for item in items if isinstance(item, dict)]
            joined = "".join(part for part in parts if part)
            if joined:
                return joined
    for key in ("message", "payload", "body", "data"):
        nested = body.get(key)
        if not isinstance(nested, dict):
            continue
        text = _extract_text(nested.get("text")) or _extract_text(nested.get("content"))
        if text:
            return text
        for inner_key in ("message", "payload", "body"):
            inner = nested.get(inner_key)
            if not isinstance(inner, dict):
                continue
            text = _extract_text(inner.get("text")) or _extract_text(inner.get("content"))
            if text:
                return text
    return None


def _select_event_body(payload: dict[str, Any]) -> dict[str, Any]:
    """兼容三种载体：平铺、``{"event": {...}}``、``{"data": {...}}``。

    合并顶层与嵌套字段：顶层保留 event_id/event_type，嵌套对象里的 text/content
    也能被读取；两边同名字段以嵌套为准。
    """
    event = payload.get("event")
    if isinstance(event, dict):
        return {**payload, **event}
    data = payload.get("data")
    if isinstance(data, dict):
        return {**payload, **data}
    return payload


@dataclass(frozen=True)
class WeComEvent:
    """订阅事件的只读视图；字段按需读取，不假设全部存在。"""

    event_id: str
    event_type: str
    conversation_key: str | None = None
    channel: str | None = None
    external_user_id: str | None = None
    external_chat_id: str | None = None
    chat_type: str | None = None
    msg_type: str | None = None
    text: str | None = None
    subject_id: str | None = None
    media: Any = None
    quote: Any = None
    external_message_id: str | None = None
    raw: dict[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: Any, headers: Mapping[str, str]) -> "WeComEvent":
        source = payload if isinstance(payload, dict) else {}
        body = _select_event_body(source)
        event_id = _as_optional_str(body.get("event_id")) or _as_optional_str(headers.get("x-wecom-event-id"))
        event_type = (
            _as_optional_str(body.get("event_type"))
            or _as_optional_str(body.get("type"))
            or _as_optional_str(headers.get("x-wecom-event"))
        )
        return cls(
            event_id=event_id or "",
            event_type=event_type or "",
            conversation_key=_as_optional_str(body.get("conversation_key")),
            channel=_as_optional_str(body.get("channel")),
            external_user_id=_as_optional_str(body.get("external_user_id")),
            external_chat_id=_as_optional_str(body.get("external_chat_id")),
            chat_type=_as_optional_str(body.get("chat_type")),
            msg_type=_as_optional_str(body.get("msg_type")),
            text=_extract_text_from_body(body),
            subject_id=_as_optional_str(body.get("subject_id")),
            media=body.get("media"),
            quote=body.get("quote"),
            external_message_id=_as_optional_str(body.get("external_message_id")),
            raw=dict(body),
        )


class EventDeduplicator:
    """进程内 TTL 去重（最小实现）。

    只在单进程内保证 24h 去重；多副本部署需换成 DB/Redis。容量上限用于防止
    长时间运行内存无界增长，超过上限时淘汰最旧条目。
    """

    def __init__(self, ttl_seconds: int = 86400, max_entries: int = 50_000) -> None:
        self._ttl = max(1, int(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._items: OrderedDict[str, float] = OrderedDict()

    def check_and_add(self, event_id: str) -> bool:
        """首次见到返回 True；TTL 内重复返回 False。"""
        now = time.monotonic()
        self._prune(now)
        seen = self._items.get(event_id)
        if seen is not None and now - seen < self._ttl:
            return False
        self._items[event_id] = now
        self._items.move_to_end(event_id)
        while len(self._items) > self._max_entries:
            self._items.popitem(last=False)
        return True

    def _prune(self, now: float) -> None:
        while self._items:
            key, seen = next(iter(self._items.items()))
            if now - seen < self._ttl:
                break
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()

    def remove(self, event_id: str) -> None:
        """允许人工重放：处理失败后移除去重标记，同 event_id 可再次进入。"""
        self._items.pop(event_id, None)

    def __len__(self) -> int:
        return len(self._items)


class WeComEventService:
    """订阅事件的服务端处理：去重、异步处理、回复能力服务。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        chat_bridge: WeComChatBridge | None = None,
        model_gateway: Any = None,
        skill_gateway: Any = None,
        short_memory: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._transport = transport
        self.chat_bridge = chat_bridge or WeComChatBridge(self.settings)
        self.model_gateway = model_gateway
        self.skill_gateway = skill_gateway
        self.short_memory = short_memory
        self.dedupe = EventDeduplicator(self.settings.wecom_dedupe_ttl_seconds)
        self.pending_actions: dict[str, str] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def subscription_secret(self) -> str:
        return _plain_secret(self.settings.wecom_sub_secret)

    @property
    def service_token(self) -> str:
        return _plain_secret(self.settings.wecom_service_token)

    @property
    def base_url(self) -> str:
        return (self.settings.wecom_service_base_url or "").strip().rstrip("/")

    def accept(self, event: WeComEvent) -> bool:
        """事件去重；必须在验签通过后调用。"""
        if not event.event_id:
            return False
        return self.dedupe.check_and_add(event.event_id)

    def enqueue(self, event: WeComEvent) -> None:
        """ACK 后异步处理，不阻塞回调响应。"""
        task = asyncio.create_task(self.process(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def process(self, event: WeComEvent) -> None:
        try:
            if event.event_type not in SUPPORTED_EVENT_TYPES:
                logger.info("wecom event ignored: unsupported type event_id=%s event_type=%s", event.event_id, event.event_type)
                return
            if self.model_gateway is not None:
                await self._process_with_agent(event)
                return
            await self._process_echo(event)
        except Exception:  # pragma: no cover - 兜底只在意外分支触发
            logger.exception("wecom event processing failed event_id=%s event_type=%s", event.event_id, event.event_type)

    async def _process_echo(self, event: WeComEvent) -> None:
        """无模型网关时的最小回显（测试/降级路径）。"""
        conversation_id = await self.chat_bridge.record_inbound(event)
        reply_text = await self.build_reply(event)
        if reply_text is None:
            logger.warning(
                "wecom event has no replyable text event_id=%s event_type=%s msg_type=%s payload_shape=%s",
                event.event_id,
                event.event_type,
                event.msg_type,
                _shape(event.raw),
            )
            return
        delivered = await self.reply(event, reply_text)
        if delivered:
            await self.chat_bridge.record_reply(conversation_id, event, reply_text)
        else:
            # 失败后清掉去重标记：能力服务不可达/鉴权失败时允许人工用同一 event_id 重放
            self.dedupe.remove(event.event_id)
            logger.warning(
                "wecom event dedupe cleared for manual replay event_id=%s event_type=%s",
                event.event_id,
                event.event_type,
            )

    async def _process_with_agent(self, event: WeComEvent) -> None:
        """把入站消息交给本地 Agent 正常对话链路（含 system prompt / 工具 / 记忆）。"""
        ref = await self.chat_bridge.ensure_conversation(event)
        if ref is None:
            logger.warning("WECOM_ALERT agent conversation unavailable; fallback echo event_id=%s", event.event_id)
            await self._process_echo(event)
            return
        ctx = SecurityContext(
            tenant_id=ref.tenant_id,
            user_id=ref.owner_user_id,
            trace_id=f"wecom-{event.event_id}",
        )
        try:
            with SessionLocal() as db:
                result = await handle_chat(
                    db,
                    ctx=ctx,
                    agent_id=ref.agent_id,
                    conversation_id=ref.conversation_id,
                    content=event.text or "",
                    content_parts=self._event_parts(event),
                    idempotency_key=event.event_id,
                    trace_id=ctx.trace_id,
                    model_gateway=self.model_gateway,
                    skill_gateway=self.skill_gateway,
                    short_memory=self.short_memory,
                )
                db.commit()
                # 用户消息由 handle_chat 写入，这里只推前端，避免重复落库
                self.chat_bridge.publish_message(ref, result.user_message)
                if result.assistant_message is not None:
                    self.chat_bridge.publish_message(ref, result.assistant_message)
                    await self.reply(event, result.assistant_message.content)
                    return
                if result.task_id is not None:
                    await self._wait_task_and_reply(event, ref, result.task_id)
                    return
                if result.pending_confirmation:
                    await self.reply(event, "该操作需要人工确认，请在控制台处理。")
                    return
                logger.warning("wecom agent produced no reply event_id=%s event_type=%s", event.event_id, event.event_type)
        except ApiError as exc:
            logger.error(
                "WECOM_ALERT agent chat failed event_id=%s event_type=%s code=%s message=%s",
                event.event_id,
                event.event_type,
                exc.code,
                exc.message,
            )
            await self.reply(event, f"处理失败：{exc.message}")
        except Exception:
            logger.exception("WECOM_ALERT agent chat crashed event_id=%s event_type=%s", event.event_id, event.event_type)
            await self.reply(event, "处理失败，请稍后重试。")

    async def _wait_task_and_reply(self, event: WeComEvent, ref: ConversationRef, task_id: Any) -> None:
        """等待 Agent 任务终态，把最终回复写回企微；Agent 已调用发送工具时不重复发。"""
        timeout = float(getattr(self.settings, "wecom_task_reply_timeout_seconds", 120.0))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with SessionLocal() as db:
                task = db.get(Task, task_id)
                if task is None:
                    logger.warning("WECOM_ALERT task missing event_id=%s task_id=%s", event.event_id, task_id)
                    return
                if task.status == "WAITING_HUMAN":
                    await self.reply(event, "该操作需要人工确认，请在控制台处理。")
                    return
                if task.status in TERMINAL_STATES:
                    message = db.scalar(
                        select(Message)
                        .where(Message.task_id == task_id, Message.sender_type == "ASSISTANT")
                        .order_by(Message.created_at.desc())
                    )
                    if message is not None:
                        self.chat_bridge.publish_message(ref, message)
                    if self._used_send_tool(db, task_id):
                        logger.info(
                            "wecom task already replied via agent tool event_id=%s task_id=%s",
                            event.event_id,
                            task_id,
                        )
                        return
                    content = message.content if message is not None else None
                    if not content and isinstance(task.result, dict):
                        content = task.result.get("content")
                    if content:
                        await self.reply(event, str(content))
                    else:
                        await self.reply(event, f"任务已结束（{task.status}），请在控制台查看详情。")
                    return
            await asyncio.sleep(1.0)
        logger.warning("WECOM_ALERT task reply timeout event_id=%s task_id=%s", event.event_id, task_id)
        await self.reply(event, "处理时间较长，请稍后在控制台查看结果。")

    @staticmethod
    def _used_send_tool(db: Any, task_id: Any) -> bool:
        """只有“回复当前会话”的工具真正成功，才认为企微侧已经收到回复。"""
        rows = db.scalars(
            select(TaskEvent).where(
                TaskEvent.task_id == task_id,
                TaskEvent.event_type == "TASK_TOOL_RESULT",
            )
        ).all()
        for row in rows:
            payload = row.payload_summary or {}
            name = payload.get("tool_name")
            if name in _WECOM_REPLY_TOOLS and payload.get("status") == "SUCCEEDED":
                return True
        return False

    @staticmethod
    def _event_parts(event: WeComEvent) -> list[dict[str, Any]]:
        """把事件元数据挂到用户消息 content_parts，供 build_context 注入模型上下文。"""
        fields = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "conversation_key": event.conversation_key,
            "channel": event.channel,
            "external_user_id": event.external_user_id,
            "external_chat_id": event.external_chat_id,
            "chat_type": event.chat_type,
            "msg_type": event.msg_type,
            "subject_id": event.subject_id,
            "external_message_id": event.external_message_id,
        }
        return [{"type": "wecom_event", **{key: value for key, value in fields.items() if value not in (None, "")}}]

    async def build_reply(self, event: WeComEvent) -> str | None:
        """最小闭环先把入站文本回显；接入中台对话时替换为 handle_chat 结果。"""
        if event.text:
            return f"已收到：{event.text}"
        return None

    async def reply(self, event: WeComEvent, text: str) -> bool:
        if not self.base_url or not self.service_token:
            logger.error(
                "WECOM_ALERT reply skipped: WECOM_SERVICE_BASE_URL / WECOM_SERVICE_TOKEN missing event_id=%s event_type=%s",
                event.event_id,
                event.event_type,
            )
            return False
        payload: dict[str, Any] = {"text": text, "idempotency_key": event.event_id}
        if event.channel == "wecom_bot":
            payload["channel"] = "wecom_bot"
        if event.conversation_key:
            payload["conversation_key"] = event.conversation_key
            return await self._send(self._url("/v1/reply"), payload, event, allow_resource_fallback=True)
        target = self._active_target(event)
        if target is None:
            logger.error(
                "WECOM_ALERT reply skipped: no conversation_key / external_user_id / external_chat_id event_id=%s event_type=%s",
                event.event_id,
                event.event_type,
            )
            return False
        payload.update(target)
        return await self._send(self._url("/v1/messages"), payload, event, allow_resource_fallback=False)

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _active_target(event: WeComEvent) -> dict[str, str] | None:
        if event.external_chat_id:
            return {"to_chat_id": event.external_chat_id}
        if event.external_user_id:
            return {"to_user_id": event.external_user_id}
        return None

    async def _send(self, url: str, payload: dict[str, Any], event: WeComEvent, *, allow_resource_fallback: bool) -> bool:
        headers = {
            "Authorization": f"Bearer {self.service_token}",
            "Content-Type": "application/json",
        }
        kwargs: dict[str, Any] = {
            "timeout": self.settings.wecom_service_timeout_seconds,
            "follow_redirects": False,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                response = await client.post(url, json=payload, headers=headers)
        except Exception as exc:
            logger.error(
                "WECOM_ALERT capability service unreachable event_id=%s event_type=%s url=%s error=%r; manual replay required",
                event.event_id,
                event.event_type,
                url,
                exc,
            )
            return False

        if 200 <= response.status_code < 300:
            logger.info(
                "wecom reply sent event_id=%s event_type=%s status=%s",
                event.event_id,
                event.event_type,
                response.status_code,
            )
            return True

        body = _safe_body(response)
        code = _error_code(body)
        if response.status_code in (401, 403) and code == "FORBIDDEN_SCOPE":
            logger.error(
                "WECOM_ALERT forbidden_scope event_id=%s event_type=%s status=%s body=%s; no retry",
                event.event_id,
                event.event_type,
                response.status_code,
                _brief(body),
            )
            return False
        if response.status_code == 403 and code == "FORBIDDEN_RESOURCE":
            logger.warning(
                "wecom reply forbidden_resource event_id=%s event_type=%s conversation_key=%s",
                event.event_id,
                event.event_type,
                event.conversation_key,
            )
            if allow_resource_fallback:
                target = self._active_target(event)
                if target is not None:
                    fallback: dict[str, Any] = {
                        "text": payload.get("text"),
                        "idempotency_key": payload.get("idempotency_key"),
                        **target,
                    }
                    if event.channel == "wecom_bot":
                        fallback["channel"] = "wecom_bot"
                    return await self._send(self._url("/v1/messages"), fallback, event, allow_resource_fallback=False)
            logger.error(
                "WECOM_ALERT FORBIDDEN_RESOURCE without fallback target event_id=%s event_type=%s",
                event.event_id,
                event.event_type,
            )
            return False
        if response.status_code == 409 and code == "CONFIRMATION_REQUIRED":
            action_id = _action_id(body)
            if action_id:
                self.pending_actions[event.event_id] = action_id
            logger.warning(
                "wecom confirmation required event_id=%s event_type=%s action_id=%s; waiting for manual confirmation",
                event.event_id,
                event.event_type,
                action_id or "<missing>",
            )
            # 已进入人工确认流程，不属于投递失败；保留去重标记，避免重复建单
            return True
        logger.error(
            "WECOM_ALERT capability reply failed event_id=%s event_type=%s status=%s code=%s body=%s",
            event.event_id,
            event.event_type,
            response.status_code,
            code or "<none>",
            _brief(body),
        )
        return False

    async def wait_idle(self, timeout: float = 5.0) -> None:
        """测试/收尾辅助：等待当前异步处理任务结束。"""
        tasks = list(self._tasks)
        if not tasks:
            return
        await asyncio.wait(tasks, timeout=timeout)


def _safe_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:500]}


def _error_code(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("code", "error_code", "error"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
        data = body.get("data")
        if isinstance(data, dict):
            for key in ("code", "error_code", "error"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
    return ""


def _action_id(body: Any) -> str:
    if isinstance(body, dict):
        value = body.get("action_id")
        if isinstance(value, str) and value:
            return value
        data = body.get("data")
        if isinstance(data, dict):
            value = data.get("action_id")
            if isinstance(value, str) and value:
                return value
    return ""


def _brief(body: Any) -> str:
    try:
        text = json.dumps(body, ensure_ascii=True, default=str)
    except Exception:
        text = str(body)
    return text[:500]


def _shape(value: Any, depth: int = 0) -> Any:
    """只输出字段名与类型，便于排查事件结构；不打印消息正文。"""
    if depth >= 3:
        return "..."
    if isinstance(value, dict):
        return {str(key): _shape(val, depth + 1) for key, val in value.items()}
    if isinstance(value, list):
        return [_shape(value[0], depth + 1)] if value else []
    return type(value).__name__
