"""企微能力服务事件订阅：验签、去重、ACK 后异步回复。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import Awaitable, Callable, Iterator
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

import app.models  # noqa: F401  # 与仓库其它集成用例一致：确保模型注册
from app.config import get_settings
from app.db.session import SessionLocal
from app.kernel.model_gateway import ModelResponse
from app.main import app
from app.models import Agent, AgentVersion, Conversation, Message, Tenant, User
from app.security.passwords import hash_password
from app.services.chat_event_bus import chat_event_bus
from app.services.wecom_chat_bridge import WeComChatBridge
from app.services.wecom_events import EventDeduplicator, WeComEvent, WeComEventService

SECRET = "test-sub-secret"
TOKEN = "test-service-token"
BASE_URL = "https://capability.example.com"
EVENT_PATH = get_settings().wecom_event_path


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    yield
    if hasattr(app.state, "wecom_event_service"):
        delattr(app.state, "wecom_event_service")


@pytest.fixture
def settings(monkeypatch):
    current = get_settings()
    monkeypatch.setattr(current, "wecom_sub_secret", SecretStr(SECRET))
    monkeypatch.setattr(current, "wecom_service_token", SecretStr(TOKEN))
    monkeypatch.setattr(current, "wecom_service_base_url", BASE_URL)
    monkeypatch.setattr(current, "wecom_dedupe_ttl_seconds", 86400)
    monkeypatch.setattr(current, "wecom_service_timeout_seconds", 5.0)
    # 默认关闭 Chat 桥接：除专门用例外的测试不写 Conversation/Message
    monkeypatch.setattr(current, "wecom_chat_enabled", False)
    return current


def _signature(secret: str, timestamp: str, body: bytes) -> str:
    message = timestamp.encode("utf-8") + b"." + body
    return "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _headers(
    body: bytes,
    *,
    event_id: str = "evt-1",
    event_type: str = "wecom.message.received",
    timestamp: str = "1700000000",
    signature: str | None = None,
) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-wecom-timestamp": timestamp,
        "x-wecom-signature": signature if signature is not None else _signature(SECRET, timestamp, body),
        "x-wecom-event": event_type,
        "x-wecom-event-id": event_id,
    }


def _payload(**overrides) -> dict:
    data = {
        "event_id": "evt-1",
        "event_type": "wecom.message.received",
        "conversation_key": "conv-1",
        "channel": "wecom_bot",
        "external_user_id": "user-1",
        "external_chat_id": "chat-1",
        "chat_type": "single",
        "msg_type": "text",
        "text": "你好",
        "external_message_id": "msg-1",
    }
    data.update(overrides)
    return data


def _install_service(
    settings,
    handler: Callable[[httpx.Request], Awaitable[httpx.Response]],
) -> tuple[WeComEventService, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    async def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return await handler(request)

    service = WeComEventService(settings, transport=httpx.MockTransport(wrapped))
    app.state.wecom_event_service = service
    return service, calls


async def _post(body: dict, headers: dict[str, str]) -> httpx.Response:
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(EVENT_PATH, content=raw, headers=headers)


async def test_valid_signature_acks_and_replies_once(settings):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    service, calls = _install_service(settings, handler)
    body = _payload()
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = _headers(raw)

    first = await _post(body, headers)
    second = await _post(body, headers)
    await service.wait_idle()

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert len(calls) == 1
    request = calls[0]
    assert request.url.path == "/v1/reply"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    sent = json.loads(request.content)
    assert sent["conversation_key"] == "conv-1"
    assert sent["idempotency_key"] == "evt-1"
    assert sent["channel"] == "wecom_bot"
    assert "你好" in sent["text"]


async def test_invalid_signature_returns_401_without_processing(settings):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    _, calls = _install_service(settings, handler)
    body = _payload()
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = _headers(raw, signature="sha256=" + "0" * 64)

    response = await _post(body, headers)

    assert response.status_code == 401
    assert calls == []


async def test_missing_conversation_key_uses_active_message_endpoint(settings):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    service, calls = _install_service(settings, handler)
    body = _payload(conversation_key=None, external_user_id="user-9", external_chat_id=None)
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

    response = await _post(body, _headers(raw))
    await service.wait_idle()

    assert response.status_code == 200
    assert len(calls) == 1
    request = calls[0]
    assert request.url.path == "/v1/messages"
    sent = json.loads(request.content)
    assert sent["to_user_id"] == "user-9"
    assert sent["idempotency_key"] == "evt-1"


async def test_forbidden_resource_falls_back_to_active_message(settings):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/reply":
            return httpx.Response(403, json={"code": "FORBIDDEN_RESOURCE"})
        return httpx.Response(200, json={"ok": True})

    service, calls = _install_service(settings, handler)
    body = _payload(conversation_key="missing-conv", external_chat_id="chat-9")
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

    response = await _post(body, _headers(raw))
    await service.wait_idle()

    assert response.status_code == 200
    assert [call.url.path for call in calls] == ["/v1/reply", "/v1/messages"]
    fallback = json.loads(calls[1].content)
    assert fallback["to_chat_id"] == "chat-9"
    assert fallback["idempotency_key"] == "evt-1"


async def test_forbidden_scope_logs_alert_without_retry(settings, caplog):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": "FORBIDDEN_SCOPE"})

    service, calls = _install_service(settings, handler)
    body = _payload()
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

    with caplog.at_level(logging.ERROR, logger="z_torque.wecom_events"):
        response = await _post(body, _headers(raw))
        await service.wait_idle()

    assert response.status_code == 200
    assert len(calls) == 1
    assert "forbidden_scope" in caplog.text
    assert "evt-1" in caplog.text


async def test_confirmation_required_is_saved_not_auto_confirmed(settings, caplog):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": "CONFIRMATION_REQUIRED", "action_id": "act-1"})

    service, calls = _install_service(settings, handler)
    body = _payload()
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

    with caplog.at_level(logging.WARNING, logger="z_torque.wecom_events"):
        response = await _post(body, _headers(raw))
        await service.wait_idle()

    assert response.status_code == 200
    assert len(calls) == 1
    assert service.pending_actions["evt-1"] == "act-1"
    assert "act-1" in caplog.text


async def test_capability_service_unreachable_logs_event_context(settings, caplog):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    service, calls = _install_service(settings, handler)
    body = _payload()
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

    with caplog.at_level(logging.ERROR, logger="z_torque.wecom_events"):
        response = await _post(body, _headers(raw))
        await service.wait_idle()

    assert response.status_code == 200
    assert len(calls) == 1
    assert "evt-1" in caplog.text
    assert "wecom.message.received" in caplog.text
    assert "connection refused" in caplog.text


async def test_failed_delivery_can_be_replayed_with_same_event_id(settings):
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary failure", request=request)
        return httpx.Response(200, json={"ok": True})

    service, calls = _install_service(settings, handler)
    body = _payload()
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = _headers(raw)

    first = await _post(body, headers)
    await service.wait_idle()
    second = await _post(body, headers)
    await service.wait_idle()

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json().get("duplicate") is not True
    assert len(calls) == 2
    assert all(call.url.path == "/v1/reply" for call in calls)


async def test_inbound_message_is_written_to_chat_conversation(settings, monkeypatch):
    tenant = Tenant(name=f"wecom-chat-{uuid4()}")
    with SessionLocal() as db:
        db.add(tenant)
        db.flush()
        owner = User(tenant_id=tenant.id, username=f"wecom-owner-{uuid4()}", password_hash=hash_password("pass"))
        db.add(owner)
        db.flush()
        agent = Agent(tenant_id=tenant.id, owner_user_id=owner.id, name="wecom-agent", source_type="USER", status="ACTIVE")
        db.add(agent)
        db.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version_number=1,
            status="PUBLISHED",
            config_schema_version="1",
            config_snapshot={"mode": "mock"},
        )
        db.add(version)
        db.flush()
        agent.current_published_version_id = version.id
        db.commit()
        owner_id = owner.id
        owner_username = owner.username
        agent_id = agent.id

    monkeypatch.setattr(settings, "wecom_chat_enabled", True)
    monkeypatch.setattr(settings, "wecom_chat_owner_username", owner_username)
    monkeypatch.setattr(settings, "wecom_chat_agent_id", str(agent_id))

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    service, calls = _install_service(settings, handler)
    body = _payload(event_id="evt-chat-1", text="企微消息入站")
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

    queue = chat_event_bus.subscribe()
    try:
        response = await _post(body, _headers(raw, event_id="evt-chat-1"))
        await service.wait_idle()
        inbound_event = await asyncio.wait_for(queue.get(), timeout=1)
        reply_event = await asyncio.wait_for(queue.get(), timeout=1)
    finally:
        chat_event_bus.unsubscribe(queue)

    assert response.status_code == 200
    assert len(calls) == 1
    assert inbound_event.sender_type == "USER"
    assert inbound_event.content == "企微消息入站"
    assert reply_event.sender_type == "ASSISTANT"
    assert "已收到：企微消息入站" in reply_event.content
    with SessionLocal() as db:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.owner_user_id == owner_id,
                Conversation.title.like("企微%"),
            )
        )
        assert conversation is not None
        assert conversation.agent_id == agent_id
        messages = db.scalars(select(Message).where(Message.conversation_id == conversation.id)).all()
        by_type = {message.sender_type: message for message in messages}
        assert by_type["USER"].content == "企微消息入站"
        assert "已收到：企微消息入站" in by_type["ASSISTANT"].content


async def test_content_field_and_nested_message_are_accepted(settings):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    service, calls = _install_service(settings, handler)
    body = {
        "event_id": "evt-content-1",
        "event_type": "wecom.bot.message.received",
        "data": {
            "conversation_key": "conv-content",
            "message": {"text": {"content": "content 字段也能识别"}},
        },
    }
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

    response = await _post(body, _headers(raw, event_id="evt-content-1", event_type="wecom.bot.message.received"))
    await service.wait_idle()

    assert response.status_code == 200
    assert len(calls) == 1
    sent = json.loads(calls[0].content)
    assert sent["conversation_key"] == "conv-content"
    assert "content 字段也能识别" in sent["text"]


async def test_agent_reply_replaces_echo(settings, monkeypatch):
    tenant = Tenant(name=f"wecom-agent-{uuid4()}")
    with SessionLocal() as db:
        db.add(tenant)
        db.flush()
        owner = User(tenant_id=tenant.id, username=f"wecom-agent-owner-{uuid4()}", password_hash=hash_password("pass"))
        db.add(owner)
        db.flush()
        agent = Agent(tenant_id=tenant.id, owner_user_id=owner.id, name="wecom-agent", source_type="USER", status="ACTIVE")
        db.add(agent)
        db.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version_number=1,
            status="PUBLISHED",
            config_schema_version="1",
            config_snapshot={"system_prompt": "你是企微助手", "model": "mock"},
        )
        db.add(version)
        db.flush()
        agent.current_published_version_id = version.id
        db.commit()
        owner_username = owner.username
        agent_id = agent.id

    class FakeGateway:
        default_model = "mock"

        async def generate(self, request):
            return ModelResponse(content="这是智能体的回答", finish_reason="stop")

    monkeypatch.setattr(settings, "wecom_chat_enabled", True)
    monkeypatch.setattr(settings, "wecom_chat_owner_username", owner_username)
    monkeypatch.setattr(settings, "wecom_chat_agent_id", str(agent_id))

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    calls: list[httpx.Request] = []

    async def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return await handler(request)

    service = WeComEventService(
        settings,
        transport=httpx.MockTransport(wrapped),
        model_gateway=FakeGateway(),
    )
    app.state.wecom_event_service = service

    body = _payload(event_id="evt-agent-1", text="帮我理解这句话")
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    response = await _post(body, _headers(raw, event_id="evt-agent-1"))
    await service.wait_idle()

    assert response.status_code == 200
    assert len(calls) == 1
    sent = json.loads(calls[0].content)
    assert sent["text"] == "这是智能体的回答"
    assert "已收到" not in sent["text"]
    with SessionLocal() as db:
        # 助手消息由 handle_chat 写入，按内容断言，避免依赖内部 id
        rows = db.scalars(select(Message).where(Message.content == "这是智能体的回答")).all()
        assert rows
        assert any(row.sender_type == "ASSISTANT" for row in rows)


async def test_split_agents_by_channel(settings, monkeypatch):
    tenant = Tenant(name=f"wecom-split-{uuid4()}")
    with SessionLocal() as db:
        db.add(tenant)
        db.flush()
        owner = User(tenant_id=tenant.id, username=f"wecom-split-owner-{uuid4()}", password_hash=hash_password("pass"))
        db.add(owner)
        db.flush()
        agents: dict[str, Agent] = {}
        for name in ("wecom-app-agent", "wecom-bot-agent"):
            agent = Agent(tenant_id=tenant.id, owner_user_id=owner.id, name=name, source_type="USER", status="ACTIVE")
            db.add(agent)
            db.flush()
            version = AgentVersion(
                tenant_id=tenant.id,
                agent_id=agent.id,
                version_number=1,
                status="PUBLISHED",
                config_schema_version="1",
                config_snapshot={"system_prompt": name, "model": "mock"},
            )
            db.add(version)
            db.flush()
            agent.current_published_version_id = version.id
            agents[name] = agent
        db.commit()
        owner_username = owner.username
        app_agent_id = agents["wecom-app-agent"].id
        bot_agent_id = agents["wecom-bot-agent"].id

    monkeypatch.setattr(settings, "wecom_chat_enabled", True)
    monkeypatch.setattr(settings, "wecom_chat_owner_username", owner_username)
    monkeypatch.setattr(settings, "wecom_chat_agent_id", "")
    monkeypatch.setattr(settings, "wecom_agent_id_app", str(app_agent_id))
    monkeypatch.setattr(settings, "wecom_agent_id_bot", str(bot_agent_id))

    bridge = WeComChatBridge(settings)
    app_event = WeComEvent(
        event_id="evt-split-app",
        event_type="wecom.message.received",
        channel="wecom_app",
        external_user_id="same-user",
        text="应用消息",
    )
    bot_event = WeComEvent(
        event_id="evt-split-bot",
        event_type="wecom.bot.message.received",
        channel="wecom_bot",
        external_user_id="same-user",
        text="机器人消息",
    )

    app_ref = await bridge.ensure_conversation(app_event)
    bot_ref = await bridge.ensure_conversation(bot_event)

    assert app_ref is not None and bot_ref is not None
    assert app_ref.conversation_id != bot_ref.conversation_id
    assert app_ref.agent_id == app_agent_id
    assert bot_ref.agent_id == bot_agent_id
    assert app_ref.title.startswith("企微应用")
    assert bot_ref.title.startswith("企微机器人")


async def test_nested_event_body_and_header_fallback(settings):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    service, calls = _install_service(settings, handler)
    body = {"event": {"conversation_key": "conv-2", "text": "嵌套消息"}}
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = _headers(raw, event_id="evt-2", event_type="wecom.bot.message.received")

    response = await _post(body, headers)
    await service.wait_idle()

    assert response.status_code == 200
    assert len(calls) == 1
    sent = json.loads(calls[0].content)
    assert sent["conversation_key"] == "conv-2"
    assert "嵌套消息" in sent["text"]


async def test_unknown_event_type_is_acked_without_reply(settings):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    service, calls = _install_service(settings, handler)
    body = _payload(event_type="wecom.unknown.event")
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

    response = await _post(body, _headers(raw, event_type="wecom.unknown.event"))
    await service.wait_idle()

    assert response.status_code == 200
    assert calls == []


async def test_missing_event_id_is_rejected(settings):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    _, calls = _install_service(settings, handler)
    body = _payload()
    body.pop("event_id")
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = _headers(raw, event_id="")

    response = await _post(body, headers)

    assert response.status_code == 400
    assert calls == []


async def test_missing_subscription_secret_returns_503(settings, monkeypatch):
    monkeypatch.setattr(settings, "wecom_sub_secret", None)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    _, calls = _install_service(settings, handler)
    body = _payload()
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    response = await _post(body, _headers(raw))

    assert response.status_code == 503
    assert calls == []


def test_dedupe_expires_after_ttl(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.services.wecom_events.time.monotonic", lambda: clock[0])
    dedupe = EventDeduplicator(ttl_seconds=10)

    assert dedupe.check_and_add("evt") is True
    assert dedupe.check_and_add("evt") is False
    clock[0] += 11
    assert dedupe.check_and_add("evt") is True
