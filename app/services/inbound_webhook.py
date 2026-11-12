"""通用入站 Webhook 服务：验签、幂等、映射会话并触发指定智能体。

约束：
- 入站请求先 ACK 再异步执行，回调方不会因为模型/工具耗时而超时；
- 幂等键落 ``zr_inbound_webhook_event``（webhook_id + event_id 唯一），重复投递不重复执行；
- 会话按 ``webhook + conversation_key`` 派生 uuid5，同一外部会话始终落同一本地会话；
- 配置了 ``reply_url`` 时，任务终态后把最终答复回写外部系统（如微信 Bridge 的 REST 发送接口）；
  智能体自身已成功调用发送工具时不重复回写。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import ApiError
from app.kernel.ports.object_store import ObjectStore
from app.kernel.security_context import SecurityContext
from app.kernel.task.states import TERMINAL_STATES
from app.models import (
    Agent,
    Conversation,
    InboundWebhook,
    InboundWebhookEvent,
    Message,
    SkillExecutionLog,
    Task,
    Tool,
    ToolVersion,
    User,
)
from app.security.secret_store import SecretStore, SecretStoreError
from app.services.bridge_media import bridge_path_for_source, export_file_to_bridge
from app.services.chat_application import handle_chat
from app.services.chat_event_bus import ChatMessageEvent, chat_event_bus
from app.services.chat_service import ChatResult
from app.services.media_fetcher import fetch_media
from app.services.media_ingest import media_kind_for_suffix, store_media
from app.services.webhook_signature import verify_signature, verify_token

logger = logging.getLogger("z_torque.inbound_webhook")

MAX_BODY_BYTES = 1_000_000
_TITLE_MAX_LEN = 200
_PATH_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")
_PATH_RE = re.compile(r"^[A-Za-z0-9_.\[\]]{1,128}$")
_DEFAULT_PLACEHOLDERS = ("[图片]", "[语音]", "[视频]", "[文件]")
_MEDIA_KINDS = frozenset({"image", "audio", "video", "file"})
# 这些工具成功执行即代表外部系统（微信侧）已经收到答复，自动回写要跳过避免重复
_REPLY_SENT_TOOLS = frozenset(
    {
        "weixin.send_text",
        "weixin.send_message",
        "weixin.send_media",
    }
)


def is_valid_path(path: str) -> bool:
    """配置项只允许点路径字符，避免把任意表达式当路径解析。"""
    return bool(_PATH_RE.fullmatch(path or ""))


def get_by_path(payload: Any, path: str) -> Any:
    """按 ``a.b[0].c`` 取 JSON 值；路径不存在返回 None。"""
    current = payload
    for name, index in _PATH_TOKEN_RE.findall(path or ""):
        if name:
            if not isinstance(current, dict):
                return None
            current = current.get(name)
        else:
            if not isinstance(current, list) or int(index) >= len(current):
                return None
            current = current[int(index)]
        if current is None:
            return None
    return current


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        for key in ("text", "content", "message"):
            text = _as_text(value.get(key))
            if text:
                return text
    try:
        return json.dumps(value, ensure_ascii=False)[:20_000]
    except (TypeError, ValueError):
        return None


def _header(headers: Any, name: str) -> str:
    value = headers.get(name) if headers is not None else None
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class WebhookConfig:
    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    name: str
    auth_mode: str
    secret: str
    signature_header: str
    timestamp_header: str
    event_id_header: str
    token_header: str
    token_prefix: str
    signature_algorithm: str
    signature_encoding: str
    signature_prefix: str
    signature_template: str
    timestamp_tolerance_seconds: int
    target_agent_id: UUID
    message_path: str
    conversation_key_path: str
    event_id_path: str
    enabled: bool
    # 可选回写目标与鉴权
    reply_url: str | None = None
    reply_token: str | None = None
    reply_account_path: str = "account_id"
    reply_peer_path: str = "peer_id"
    # 媒体映射：media_path 为空表示不解析媒体
    media_path: str | None = None
    media_type_path: str = "type"
    media_source_path: str = "filePath"
    media_mime_path: str = "mimeType"
    media_file_name_path: str = "file_name"
    media_placeholder_texts: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ParsedMedia:
    kind: str
    source: str
    file_name: str
    mime_type: str | None = None
    # 所属消息：用于一次发送内的媒体去重与审计
    message_id: str | None = None
    media_id: str | None = None
    # url 不可用时的本地回退来源（同机 / 共享卷部署）
    fallback_source: str | None = None


@dataclass(frozen=True)
class ParsedInbound:
    event_id: str
    text: str
    conversation_key: str
    # 回写目标（从 payload 按 reply_*_path 提取）
    reply_account: str | None = None
    reply_peer: str | None = None
    media: tuple[ParsedMedia, ...] = ()
    # 一次发送（turn）的元数据：只用于上下文/审计，不参与文本拼接
    quote: str | None = None
    quote_message_id: str | None = None
    context_token: str | None = None
    turn_id: str | None = None
    message_ids: tuple[str, ...] = ()


def _normalize_media_kind(value: Any, file_name: str) -> str:
    kind = str(value or "").strip().lower()
    if kind == "voice":
        return "audio"
    if kind in _MEDIA_KINDS:
        return kind
    return media_kind_for_suffix(Path(file_name).suffix) or "file"


def _media_entries(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _collect_media_entries(config: WebhookConfig, payload: Any) -> list[dict[str, Any]]:
    """收集一次发送的全部媒体。

    顺序：顶层 ``media_list``（新契约）→ ``messages[].media``（逐条明细）
    → 配置的 ``media_path``（旧 ``message.media`` 行为）。
    新契约的显式媒体节点始终解析；未配置 ``media_path`` 时不再回退读取旧
    ``message.media``，保持纯文本 Webhook 的原有语义。
    """
    if not isinstance(payload, dict):
        return []
    entries = _media_entries(payload.get("media_list"))
    if entries:
        return entries
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                entries.extend(_media_entries(message.get("media")))
        if entries:
            return entries
    if not config.media_path:
        return []
    return _media_entries(get_by_path(payload, config.media_path))


def _resolve_media_source(config: WebhookConfig, entry: dict[str, Any]) -> tuple[str | None, str | None]:
    """返回 (首选来源, 本地回退来源)：优先 url，其次配置路径 / filePath。"""
    url = _as_text(entry.get("url"))
    path = _as_text(get_by_path(entry, config.media_source_path)) or _as_text(entry.get("filePath"))
    source = url or path
    fallback = path if url and path and path != url else None
    return source, fallback


def _parse_media(config: WebhookConfig, payload: Any) -> tuple[ParsedMedia, ...]:
    parsed: list[ParsedMedia] = []
    seen: set[tuple[str, str]] = set()
    for entry in _collect_media_entries(config, payload):
        source, fallback_source = _resolve_media_source(config, entry)
        if not source:
            continue
        message_id = _as_text(entry.get("messageId")) or _as_text(entry.get("message_id"))
        media_id = _as_text(entry.get("mediaId")) or _as_text(entry.get("media_id"))
        # 同一 URL 在一次发送里重复出现时只处理一次
        dedupe_key = (media_id or message_id or "", source)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        file_name = (
            _as_text(get_by_path(entry, config.media_file_name_path))
            or _as_text(entry.get("fileName"))
            or Path(source).name
            or "attachment"
        )[:255]
        parsed.append(
            ParsedMedia(
                kind=_normalize_media_kind(
                    get_by_path(entry, config.media_type_path) or entry.get("type"),
                    file_name,
                ),
                source=source,
                file_name=file_name,
                mime_type=_as_text(get_by_path(entry, config.media_mime_path)) or _as_text(entry.get("mimeType")),
                message_id=message_id,
                media_id=media_id,
                fallback_source=fallback_source,
            )
        )
    return tuple(parsed)


def _extract_quote(payload: Any) -> tuple[str | None, str | None]:
    """一次发送的引用上下文只取 ``message.quote``；``messages[].quote`` 仅作明细。"""
    if not isinstance(payload, dict):
        return None, None
    quote = payload.get("quote")
    message = payload.get("message")
    if isinstance(message, dict) and message.get("quote") is not None:
        quote = message.get("quote")
    if isinstance(quote, str):
        return _as_text(quote), None
    if isinstance(quote, dict):
        text = _as_text(quote.get("text")) or _as_text(quote.get("content"))
        message_id = _as_text(quote.get("messageId")) or _as_text(quote.get("message_id"))
        return text, message_id
    return None, None


def _extract_context_token(payload: Any) -> str | None:
    """回写使用本次发送最后一个 token（``message.context_token``），不读逐条 token。"""
    if not isinstance(payload, dict):
        return None
    token = _as_text(get_by_path(payload, "message.context_token"))
    if token:
        return token
    return _as_text(payload.get("context_token")) or _as_text(get_by_path(payload, "data.context_token"))


def _extract_turn_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    turn_id = _as_text(payload.get("turn_id"))
    if turn_id:
        return turn_id
    first_message_id = _as_text(get_by_path(payload, "message.message_id"))
    return f"turn_{first_message_id}" if first_message_id else None


def _extract_message_ids(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    values = payload.get("message_ids")
    if isinstance(values, list):
        ids = tuple(item for item in (_as_text(value) for value in values) if item)
        if ids:
            return ids
    first_message_id = _as_text(get_by_path(payload, "message.message_id"))
    return (first_message_id,) if first_message_id else ()


async def _fetch_media_bytes(
    media: ParsedMedia,
    *,
    roots: list[Path],
    max_bytes: int,
    timeout: float,
) -> bytes:
    """优先按 url 获取；url 不可用时退回同机 / 共享卷的本地路径。"""
    try:
        return await fetch_media(media.source, roots=roots, max_bytes=max_bytes, timeout=timeout)
    except ApiError:
        if not media.fallback_source:
            raise
        return await fetch_media(media.fallback_source, roots=roots, max_bytes=max_bytes, timeout=timeout)


class InboundWebhookService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | None = None,
        model_gateway: Any = None,
        skill_gateway: Any = None,
        short_memory: Any = None,
        object_store: ObjectStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.model_gateway = model_gateway
        self.skill_gateway = skill_gateway
        self.short_memory = short_memory
        # 媒体落库经 object.store Port；纯文本 Webhook 不要求注入
        self.object_store = object_store
        self._tasks: set[asyncio.Task[Any]] = set()
        # 任务型回复的后台观察器：等任务终态后把最终回复推给控制台
        self._watchers: set[asyncio.Task[Any]] = set()

    # ---- 读取与校验 -------------------------------------------------
    def load(self, webhook_id: UUID) -> WebhookConfig | None:
        with self._session() as db:
            item = db.get(InboundWebhook, webhook_id)
            if item is None:
                return None
            try:
                secret = SecretStore().decrypt(item.secret_ciphertext)
            except SecretStoreError:
                logger.error("WECOM_ALERT inbound webhook secret cannot be decrypted webhook_id=%s", webhook_id)
                return None
            reply_token: str | None = None
            if item.reply_token_ciphertext:
                try:
                    reply_token = SecretStore().decrypt(item.reply_token_ciphertext)
                except SecretStoreError:
                    logger.error(
                        "WECOM_ALERT inbound webhook reply token cannot be decrypted webhook_id=%s",
                        webhook_id,
                    )
            return WebhookConfig(
                id=item.id,
                tenant_id=item.tenant_id,
                owner_user_id=item.owner_user_id,
                name=item.name,
                auth_mode=item.auth_mode,
                secret=secret,
                signature_header=item.signature_header,
                timestamp_header=item.timestamp_header,
                event_id_header=item.event_id_header,
                token_header=item.token_header,
                token_prefix=item.token_prefix,
                signature_algorithm=item.signature_algorithm,
                signature_encoding=item.signature_encoding,
                signature_prefix=item.signature_prefix,
                signature_template=item.signature_template,
                timestamp_tolerance_seconds=item.timestamp_tolerance_seconds,
                target_agent_id=item.target_agent_id,
                message_path=item.message_path,
                conversation_key_path=item.conversation_key_path,
                event_id_path=item.event_id_path,
                reply_url=(item.reply_url or "").strip() or None,
                reply_token=reply_token,
                reply_account_path=item.reply_account_path or "account_id",
                reply_peer_path=item.reply_peer_path or "peer_id",
                enabled=bool(item.enabled),
                media_path=(item.media_path or "").strip() or None,
                media_type_path=item.media_type_path or "type",
                media_source_path=item.media_source_path or "filePath",
                media_mime_path=item.media_mime_path or "mimeType",
                media_file_name_path=item.media_file_name_path or "file_name",
                media_placeholder_texts=tuple(item.media_placeholder_texts or ()) or None,
            )

    @staticmethod
    def authenticate(config: WebhookConfig, raw_body: bytes, headers: Any) -> bool:
        if config.auth_mode == "TOKEN":
            return verify_token(
                config.secret,
                _header(headers, config.token_header),
                config.token_prefix,
            )
        return verify_signature(
            secret=config.secret,
            timestamp=_header(headers, config.timestamp_header),
            signature=_header(headers, config.signature_header),
            raw_body=raw_body,
            algorithm=config.signature_algorithm,
            encoding=config.signature_encoding,
            prefix=config.signature_prefix,
            template=config.signature_template,
            tolerance_seconds=config.timestamp_tolerance_seconds,
        )

    def parse(self, config: WebhookConfig, payload: Any, headers: Any, raw_body: bytes) -> ParsedInbound | None:
        event_id = _as_text(get_by_path(payload, config.event_id_path)) or _header(headers, config.event_id_header)
        if not event_id:
            # 新契约固定提供确定性 event_id；旧 payload 回退到 message_id
            for path in ("event_id", "data.event_id", "message.message_id"):
                event_id = _as_text(get_by_path(payload, path))
                if event_id:
                    break
        if not event_id:
            # 未提供事件 id 时用请求体哈希：回调方重试同一请求仍可幂等
            event_id = hashlib.sha256(raw_body).hexdigest()[:32]
        # 顶层 text / message.text 已是本次发送的合并文本，直接使用，不拼接 messages[].text
        text = _as_text(get_by_path(payload, config.message_path)) or ""
        if not text:
            for path in ("text", "data.text", "data.content", "message.text", "message.content"):
                text = _as_text(get_by_path(payload, path)) or ""
                if text:
                    break
        media = _parse_media(config, payload)
        # 图片/语音占位文本不是真实用户输入；有附件时置空，无附件时保留原文本
        if text and text in set(config.media_placeholder_texts or _DEFAULT_PLACEHOLDERS):
            text = "" if media else text
        if not text and not media:
            return None
        key = _as_text(get_by_path(payload, config.conversation_key_path)) or "default"
        quote, quote_message_id = _extract_quote(payload)
        return ParsedInbound(
            event_id=event_id[:128],
            text=text,
            conversation_key=key[:200],
            reply_account=_as_text(get_by_path(payload, config.reply_account_path)),
            reply_peer=_as_text(get_by_path(payload, config.reply_peer_path)),
            media=media,
            quote=quote,
            quote_message_id=quote_message_id,
            context_token=_extract_context_token(payload),
            turn_id=_extract_turn_id(payload),
            message_ids=_extract_message_ids(payload),
        )

    # ---- 幂等占位 ---------------------------------------------------
    def claim(self, config: WebhookConfig, event_id: str) -> UUID | None:
        """首次投递返回事件行 id；重复投递/并发冲突返回 None。"""
        try:
            with self._session() as db:
                row = InboundWebhookEvent(
                    tenant_id=config.tenant_id,
                    webhook_id=config.id,
                    event_id=event_id,
                    status="ACCEPTED",
                )
                db.add(row)
                webhook = db.get(InboundWebhook, config.id)
                if webhook is not None:
                    webhook.last_received_at = datetime.now(timezone.utc)
                db.commit()
                return row.id
        except IntegrityError:
            return None

    # ---- 异步处理 ---------------------------------------------------
    def enqueue(self, config: WebhookConfig, event_row_id: UUID, event_id: str, text: str, conversation_key: str, *, parsed: ParsedInbound | None = None) -> None:
        task = asyncio.create_task(self.process(config, event_row_id, event_id, text, conversation_key, parsed=parsed))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def process(
        self,
        config: WebhookConfig,
        event_row_id: UUID,
        event_id: str,
        text: str,
        conversation_key: str,
        *,
        parsed: ParsedInbound | None = None,
    ) -> None:
        try:
            conversation_id = await self._process_async(
                config, event_row_id, event_id, text, conversation_key, parsed=parsed
            )
        except Exception as exc:  # 后台任务必须自己收口，异常也要落到事件行
            logger.exception(
                "WECOM_ALERT inbound webhook processing failed webhook_id=%s event_id=%s",
                config.id,
                event_id,
            )
            self._mark_failed(event_row_id, str(exc)[:500])
            return
        self._mark_completed(event_row_id, conversation_id)

    async def _process_async(
        self,
        config: WebhookConfig,
        event_row_id: UUID,
        event_id: str,
        text: str,
        conversation_key: str,
        *,
        parsed: ParsedInbound | None = None,
    ) -> UUID:
        with self._session() as db:
            row = db.get(InboundWebhookEvent, event_row_id)
            if row is not None:
                row.status = "PROCESSING"
                db.commit()
            owner = db.get(User, config.owner_user_id)
            if owner is None or owner.tenant_id != config.tenant_id or not owner.is_active:
                raise ApiError("AI_WEBHOOK_OWNER_UNAVAILABLE", "webhook owner is unavailable", 409)
            agent = db.get(Agent, config.target_agent_id)
            if (
                agent is None
                or agent.tenant_id != config.tenant_id
                or agent.status != "ACTIVE"
                or agent.current_published_version_id is None
            ):
                raise ApiError("AI_WEBHOOK_AGENT_UNAVAILABLE", "webhook target agent is unavailable", 409)
            conversation = self._ensure_conversation(db, config, conversation_key)
            ctx = SecurityContext(
                tenant_id=config.tenant_id,
                user_id=owner.id,
                trace_id=f"webhook-{config.id}-{event_id}"[:128],
            )
            owner_user_id = conversation.owner_user_id
            conversation_id = conversation.id
            title = conversation.title or config.name
            event_part: dict[str, Any] = {
                "type": "webhook_event",
                "webhook_id": str(config.id),
                "event_id": event_id,
                "conversation_key": conversation_key,
            }
            if parsed is not None:
                if parsed.turn_id:
                    event_part["turn_id"] = parsed.turn_id
                if parsed.quote:
                    event_part["quote"] = parsed.quote
                if parsed.quote_message_id:
                    event_part["quote_message_id"] = parsed.quote_message_id
            content_parts: list[dict[str, Any]] = [event_part]
            content_parts.extend(await self._ingest_media(db, config, conversation, parsed))
            result = await self._handle_chat(db, ctx, config, conversation, event_id, text, conversation_key, content_parts)
            db.commit()
            # 入站消息与同步回复落库后立即推给控制台；任务型回复由前端按 pending_task_id 订阅任务事件流展示执行过程
            self._publish_message(owner_user_id, conversation_id, title, result.user_message, pending_task_id=result.task_id)
            if result.assistant_message is not None:
                self._publish_message(owner_user_id, conversation_id, title, result.assistant_message)
                await self._reply_to_source(config, parsed, result.assistant_message.content)
            elif result.task_id is not None and config.reply_url:
                self._watch_task_reply(result.task_id, config=config, parsed=parsed)
            return conversation_id

    async def _handle_chat(
        self,
        db: Session,
        ctx: SecurityContext,
        config: WebhookConfig,
        conversation: Conversation,
        event_id: str,
        text: str,
        conversation_key: str,
        content_parts: list[dict[str, Any]],
    ) -> ChatResult:
        if self.model_gateway is None:
            raise ApiError("AI_MODEL_NOT_AVAILABLE", "model gateway is unavailable", 503)
        # 与企微链路一致：在事件循环内直接 await，复用同一个模型/工具网关
        return await handle_chat(
            db,
            ctx=ctx,
            agent_id=conversation.agent_id,
            conversation_id=conversation.id,
            content=text,
            content_parts=content_parts,
            idempotency_key=f"webhook:{config.id}:{hashlib.sha256(event_id.encode('utf-8')).hexdigest()[:24]}",
            trace_id=ctx.trace_id,
            model_gateway=self.model_gateway,
            skill_gateway=self.skill_gateway,
            short_memory=self.short_memory,
            commit_before_generate=True,
        )

    async def _ingest_media(
        self,
        db: Session,
        config: WebhookConfig,
        conversation: Conversation,
        parsed: ParsedInbound | None,
    ) -> list[dict[str, Any]]:
        """按 Webhook 媒体映射获取并落库附件，返回 content_parts。

        一次发送可能包含多个媒体：全部逐个处理；同一媒体（mediaId/messageId +
        文件内容）在一次请求内只落库一次。
        """
        if parsed is None or not parsed.media:
            return []
        if self.object_store is None:
            raise ApiError("AI_CAPABILITY_UNAVAILABLE", "object store is unavailable", 503)
        settings = get_settings()
        if len(parsed.media) > settings.inbound_max_files:
            raise ApiError("AI_FILE_TOO_LARGE", "too many attachments", 413)
        max_bytes = settings.inbound_max_file_mb * 1024 * 1024
        roots = [Path(item) for item in settings.inbound_media_roots]
        allowed = {item.lower() for item in settings.inbound_allowed_media_types}
        parts: list[dict[str, Any]] = []
        seen_sources: set[str] = set()
        seen_content: set[tuple[str, str]] = set()
        for media in parsed.media:
            # 同一 URL / 路径在一次发送里只下载一次
            if media.source in seen_sources:
                continue
            seen_sources.add(media.source)
            identity = media.media_id or media.message_id or media.source
            declared = (media.mime_type or "").split(";")[0].strip().lower()
            if declared and not declared.endswith("/*") and declared != "application/octet-stream" and declared not in allowed:
                raise ApiError("AI_FILE_TYPE_UNSUPPORTED", "attachment mime type is not allowed", 415)
            data = await _fetch_media_bytes(
                media,
                roots=roots,
                max_bytes=max_bytes,
                timeout=settings.inbound_media_timeout_seconds,
            )
            # 同一个 URL 在 media_list / messages[].media 中重复出现时，内容相同即跳过
            content_key = (identity, hashlib.sha256(data).hexdigest())
            if content_key in seen_content:
                continue
            seen_content.add(content_key)
            item = store_media(
                db,
                self.object_store,
                tenant_id=config.tenant_id,
                owner_user_id=conversation.owner_user_id,
                conversation_id=conversation.id,
                file_name=media.file_name,
                media_type=media.kind,
                stream=io.BytesIO(data),
                max_bytes=max_bytes,
                mime_type=media.mime_type,
            )
            # 入站媒体通常已在 Bridge 共享卷里，直接映射为容器内可发送路径；
            # URL-only 来源则复制一份到共享卷，让后续 weixin.send_media 可用。
            bridge_path = bridge_path_for_source(media.fallback_source or media.source, settings)
            if bridge_path is None and settings.bridge_media_root is not None:
                try:
                    bridge_path = export_file_to_bridge(item, self.object_store, settings).bridge_path
                except ApiError:
                    logger.warning("bridge media export skipped file_id=%s", item.id, exc_info=True)
            part = {
                "type": media.kind,
                "file_id": str(item.id),
                "file_name": item.original_name,
                "mime_type": item.media_type,
            }
            if bridge_path:
                part["bridge_path"] = bridge_path
            parts.append(part)
        return parts

    def _ensure_conversation(self, db: Session, config: WebhookConfig, conversation_key: str) -> Conversation:
        conversation_id = uuid5(
            NAMESPACE_URL,
            f"z-torque:inbound-webhook:conversation:{config.tenant_id}:{config.id}:{conversation_key}",
        )
        conversation = db.get(Conversation, conversation_id)
        now = datetime.now(timezone.utc)
        title = f"{config.name} · {conversation_key}"[:_TITLE_MAX_LEN]
        if conversation is None:
            conversation = Conversation(
                id=conversation_id,
                tenant_id=config.tenant_id,
                owner_user_id=config.owner_user_id,
                agent_id=config.target_agent_id,
                title=title,
                status="ACTIVE",
                last_message_at=now,
            )
            db.add(conversation)
            db.flush()
            return conversation
        if conversation.tenant_id != config.tenant_id or conversation.owner_user_id != config.owner_user_id:
            raise ApiError("AI_WEBHOOK_CONVERSATION_CONFLICT", "conversation owner mismatch", 409)
        if conversation.status == "ARCHIVED":
            conversation.status = "ACTIVE"
            conversation.archived_at = None
        # 目标智能体被重新配置时，让同一外部会话切到新 Agent；handle_chat 不再校验旧值
        conversation.agent_id = config.target_agent_id
        conversation.last_message_at = now
        return conversation

    @staticmethod
    def _publish_message(
        owner_user_id: UUID,
        conversation_id: UUID,
        title: str,
        message: Message,
        *,
        pending_task_id: UUID | None = None,
    ) -> None:
        """把已落库的消息推给控制台会话页（不重复写库）。"""
        chat_event_bus.publish(
            ChatMessageEvent(
                owner_user_id=owner_user_id,
                conversation_id=conversation_id,
                message_id=message.id,
                sender_type=message.sender_type,
                content=message.content or "",
                content_parts=message.content_parts,
                title=title,
                created_at=message.created_at or datetime.now(timezone.utc),
                task_id=message.task_id,
                pending_task_id=pending_task_id,
            )
        )

    def _watch_task_reply(
        self,
        task_id: UUID,
        *,
        config: WebhookConfig,
        parsed: ParsedInbound | None,
    ) -> None:
        """工具型任务完成后把最终答复回写外部系统；控制台执行过程由前端任务事件流负责。"""
        watcher = asyncio.create_task(
            self._watch_task_message(task_id, config=config, parsed=parsed)
        )
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)

    async def _watch_task_message(
        self,
        task_id: UUID,
        *,
        config: WebhookConfig,
        parsed: ParsedInbound | None,
    ) -> None:
        timeout = float(get_settings().inbound_webhook_task_watch_seconds)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            finished = False
            reply_text: str | None = None
            with self._session() as db:
                task = db.get(Task, task_id)
                if task is None:
                    return
                if task.status in TERMINAL_STATES:
                    message = db.scalar(
                        select(Message)
                        .where(Message.task_id == task_id, Message.sender_type == "ASSISTANT")
                        .order_by(Message.created_at.desc())
                    )
                    if self._used_send_tool(db, task_id):
                        logger.info(
                            "inbound webhook task already replied via agent tool webhook_id=%s task_id=%s",
                            config.id,
                            task_id,
                        )
                        return
                    content = message.content if message is not None else None
                    if not content and isinstance(task.result, dict):
                        content = task.result.get("content")
                    reply_text = str(content) if content else None
                    finished = True
            if finished:
                # 先关闭数据库会话，再做外部 HTTP 调用，避免长时间占用连接
                await self._reply_to_source(config, parsed, reply_text)
                return
            await asyncio.sleep(1.0)
        logger.warning("WECOM_ALERT inbound webhook task watch timeout task_id=%s", task_id)

    @staticmethod
    def _used_send_tool(db: Session, task_id: UUID) -> bool:
        """智能体已成功调用发送工具时，外部系统已经收到答复，无需再回写。"""
        count = db.scalar(
            select(func.count())
            .select_from(SkillExecutionLog)
            .join(ToolVersion, ToolVersion.id == SkillExecutionLog.tool_version_id)
            .join(Tool, Tool.id == ToolVersion.tool_id)
            .where(
                SkillExecutionLog.task_id == task_id,
                SkillExecutionLog.status == "SUCCEEDED",
                Tool.name.in_(_REPLY_SENT_TOOLS),
            )
        )
        return bool(count)

    async def _reply_to_source(
        self,
        config: WebhookConfig,
        parsed: ParsedInbound | None,
        text: str | None,
    ) -> None:
        """把最终答复 POST 回外部系统（微信 Bridge 的 /v1/messages/send）。"""
        if not config.reply_url:
            return
        if parsed is None or not parsed.reply_account or not parsed.reply_peer:
            logger.warning(
                "WECOM_ALERT inbound webhook reply skipped: account/peer not found webhook_id=%s account_path=%s peer_path=%s",
                config.id,
                config.reply_account_path,
                config.reply_peer_path,
            )
            return
        content = (text or "").strip()
        if not content:
            return
        headers = {"Content-Type": "application/json"}
        if config.reply_token:
            headers["Authorization"] = f"Bearer {config.reply_token}"
        payload = {
            "accountId": parsed.reply_account,
            "peerId": parsed.reply_peer,
            "text": content,
        }
        # 回写使用本次发送最后一个 context_token，保证回复落在同一会话上下文
        if parsed.context_token:
            payload["contextToken"] = parsed.context_token
        timeout = float(get_settings().inbound_webhook_reply_timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(config.reply_url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "WECOM_ALERT inbound webhook reply error webhook_id=%s peer=%s error=%s",
                config.id,
                parsed.reply_peer,
                exc,
            )
            return
        if response.status_code >= 400:
            logger.warning(
                "WECOM_ALERT inbound webhook reply failed webhook_id=%s peer=%s status=%s body=%s",
                config.id,
                parsed.reply_peer,
                response.status_code,
                (response.text or "")[:200],
            )
            return
        logger.info(
            "inbound webhook reply delivered webhook_id=%s peer=%s",
            config.id,
            parsed.reply_peer,
        )

    # ---- 状态回写 ---------------------------------------------------
    def _mark_completed(self, event_row_id: UUID, conversation_id: UUID) -> None:
        self._update_event(event_row_id, status="COMPLETED", conversation_id=conversation_id)

    def _mark_failed(self, event_row_id: UUID, error: str) -> None:
        self._update_event(event_row_id, status="FAILED", error=error)

    def _update_event(self, event_row_id: UUID, *, status: str, error: str | None = None, conversation_id: UUID | None = None) -> None:
        try:
            with self._session() as db:
                row = db.get(InboundWebhookEvent, event_row_id)
                if row is None:
                    return
                row.status = status
                if error is not None:
                    row.error = error
                if conversation_id is not None:
                    row.conversation_id = conversation_id
                db.commit()
        except Exception:
            logger.exception("WECOM_ALERT inbound webhook event update failed event_row_id=%s", event_row_id)

    async def wait_idle(self, timeout: float = 5.0) -> None:
        tasks = list(self._tasks)
        if not tasks:
            return
        await asyncio.wait(tasks, timeout=timeout)

    def _session(self) -> Session:
        if self._session_factory is not None:
            return self._session_factory()
        from app.db.session import SessionLocal

        return SessionLocal()
