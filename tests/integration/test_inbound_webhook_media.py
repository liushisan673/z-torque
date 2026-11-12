"""现有 /webhooks/{id} 入口的多媒体能力：媒体落库 + content_parts + 占位文本处理。"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.builtin_plugins.object_store import LocalObjectStore
from app.config import get_settings
from app.db.session import SessionLocal
from app.kernel.model_gateway import ModelResponse
from app.models import (
    Agent,
    AgentVersion,
    Conversation,
    File,
    InboundWebhook,
    InboundWebhookEvent,
    Message,
    Tenant,
    User,
)
from app.models.message import message_file
from app.security.passwords import hash_password
from app.services.inbound_webhook import InboundWebhookService, WebhookConfig


class _FakeGateway:
    default_model = "mock"

    async def generate(self, request):
        return ModelResponse(content="已收到图片", finish_reason="stop")


@pytest.fixture()
def webhook_media_env(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "inbound_media_roots", [str(tmp_path)])
    # 测试隔离：出站共享卷也固定到临时目录，避免写入项目 data/bridge-media
    monkeypatch.setattr(settings, "bridge_media_shared_root", tmp_path)
    monkeypatch.setattr(settings, "bridge_media_container_root", "/tmp/weixin-agent/media")
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")

    with SessionLocal() as db:
        tenant = Tenant(name=f"webhook-media-{uuid4()}")
        db.add(tenant)
        db.flush()
        owner = User(tenant_id=tenant.id, username=f"webhook-media-{uuid4()}", password_hash=hash_password("pass"))
        db.add(owner)
        db.flush()
        agent = Agent(tenant_id=tenant.id, owner_user_id=owner.id, name="media-agent", source_type="USER", status="ACTIVE")
        db.add(agent)
        db.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version_number=1,
            status="PUBLISHED",
            config_schema_version="1",
            config_snapshot={"system_prompt": "你是媒体助手", "model": "mock"},
        )
        db.add(version)
        db.flush()
        agent.current_published_version_id = version.id
        webhook = InboundWebhook(tenant_id=tenant.id, owner_user_id=owner.id, name="微信回调", secret_ciphertext="unused", target_agent_id=agent.id)
        db.add(webhook)
        db.flush()
        event = InboundWebhookEvent(tenant_id=tenant.id, webhook_id=webhook.id, event_id="evt-media-1")
        db.add(event)
        db.commit()
        ids = {
            "tenant": tenant.id,
            "owner": owner.id,
            "agent": agent.id,
            "webhook": webhook.id,
            "event": event.id,
        }

    config = WebhookConfig(
        id=ids["webhook"],
        tenant_id=ids["tenant"],
        owner_user_id=ids["owner"],
        name="微信回调",
        auth_mode="HMAC",
        secret="unused",
        signature_header="X-Webhook-Signature",
        timestamp_header="X-Webhook-Timestamp",
        event_id_header="X-Webhook-Event-Id",
        token_header="Authorization",
        token_prefix="Bearer ",
        signature_algorithm="SHA256",
        signature_encoding="HEX",
        signature_prefix="sha256=",
        signature_template="{timestamp}.{body}",
        timestamp_tolerance_seconds=300,
        target_agent_id=ids["agent"],
        message_path="message.text",
        conversation_key_path="conversation_key",
        event_id_path="event_id",
        enabled=True,
        media_path="message.media",
        media_type_path="type",
        media_source_path="filePath",
        media_mime_path="mimeType",
        media_file_name_path="file_name",
    )
    service = InboundWebhookService(session_factory=SessionLocal, model_gateway=_FakeGateway(), object_store=LocalObjectStore(tmp_path / "objects"))
    yield service, config, ids, image

    # 只清理本用例租户，避免误删其他用例留下的父行导致 FK 失败
    with SessionLocal.begin() as db:
        db.execute(delete(message_file).where(message_file.c.tenant_id == ids["tenant"]))
        for model in (Message, File, InboundWebhookEvent, InboundWebhook, Conversation, AgentVersion, Agent, User):
            db.execute(delete(model).where(model.tenant_id == ids["tenant"]))
        db.execute(delete(Tenant).where(Tenant.id == ids["tenant"]))


def _payload(image_path) -> dict:
    return {
        "event_id": "evt-media-1",
        "conversation_key": "peer-1",
        "message": {
            "message_id": "m-media-1",
            "text": "[图片]",
            "media": {"type": "image", "filePath": str(image_path), "mimeType": "image/*"},
        },
    }


async def test_webhook_media_is_ingested_into_content_parts(webhook_media_env):
    service, config, ids, image = webhook_media_env
    raw = _payload(image)
    parsed = service.parse(config, raw, {}, b"{}")
    assert parsed is not None
    assert parsed.text == ""
    assert len(parsed.media) == 1

    await service.process(config, ids["event"], parsed.event_id, parsed.text, parsed.conversation_key, parsed=parsed)

    with SessionLocal() as db:
        message = db.scalar(select(Message).where(Message.tenant_id == ids["tenant"], Message.sender_type == "USER"))
        assert message is not None and message.content == ""
        image_parts = [part for part in (message.content_parts or []) if part.get("type") == "image"]
        assert image_parts and image_parts[0]["file_name"] == "photo.jpg"
        assert image_parts[0]["bridge_path"] == "/tmp/weixin-agent/media/photo.jpg"
        item = db.get(File, image_parts[0]["file_id"])
        assert item is not None and item.status == "UPLOADED"
        assert item.conversation_id == message.conversation_id


async def test_webhook_without_media_path_keeps_text_behavior(webhook_media_env):
    service, config, _ids, image = webhook_media_env
    config = replace(config, media_path=None)
    raw = _payload(image)
    raw["message"]["text"] = "在吗"
    parsed = service.parse(config, raw, {}, b"{}")
    assert parsed is not None
    assert parsed.text == "在吗"
    assert parsed.media == ()


def _media_item(path, *, message_id: str, url: str | None = None) -> dict:
    item = {
        "type": "image",
        "filePath": str(path),
        "mimeType": "image/*",
        "messageId": message_id,
    }
    if url:
        item["url"] = url
    return item


def _turn_payload(
    *,
    event_id: str,
    text: str,
    message_ids: list[str],
    media_list: list[dict] | None = None,
    messages: list[dict] | None = None,
    quote: dict | None = None,
    context_token: str | None = "ctx-1",
    conversation_key: str = "peer-1",
) -> dict:
    """按 wechat-Bot 新契约构造一次发送（turn）的 webhook body。"""
    media_list = media_list or []
    messages = messages or []
    first_media = media_list[0] if media_list else None
    return {
        "event": "message.inbound",
        "turn_id": f"turn_{message_ids[0]}" if message_ids else None,
        "event_id": event_id,
        "text": text,
        "message_ids": message_ids,
        "media_list": media_list,
        "messages": messages,
        "message": {
            "message_id": message_ids[0] if message_ids else None,
            "text": text,
            "quote": quote,
            "media": first_media,
            "context_token": context_token,
            "timestamp": 1794359899495,
        },
        "session": {"has_context_token": bool(context_token)},
        "account_id": "account-1",
        "peer_id": conversation_key,
        "conversation_key": conversation_key,
        "data": {
            "content": text,
            "text": text,
            "conversation_key": conversation_key,
            "event_id": event_id,
            "media_url": first_media.get("url") if first_media else None,
        },
    }


async def test_turn_with_image_and_text_is_one_message(webhook_media_env):
    service, config, ids, image = webhook_media_env
    media = _media_item(image, message_id="m-1")
    raw = _turn_payload(
        event_id="evt-turn-1",
        text="这是什么",
        message_ids=["m-1", "m-2"],
        media_list=[media],
        messages=[
            {"message_id": "m-1", "seq": 43, "text": "[图片]", "quote": None, "media": [media]},
            {
                "message_id": "m-2",
                "seq": 44,
                "text": "这是什么",
                "quote": {"messageId": "q-2", "text": "明细引用"},
                "media": [],
            },
        ],
        quote={"messageId": "q-1", "text": "订单 A123"},
    )

    parsed = service.parse(config, raw, {}, b"{}")
    assert parsed is not None
    assert parsed.event_id == "evt-turn-1"
    assert parsed.turn_id == "turn_m-1"
    assert parsed.text == "这是什么"
    assert parsed.message_ids == ("m-1", "m-2")
    # 引用上下文只取 message.quote，不用 messages[].quote
    assert parsed.quote == "订单 A123"
    assert parsed.quote_message_id == "q-1"
    assert len(parsed.media) == 1

    await service.process(config, ids["event"], parsed.event_id, parsed.text, parsed.conversation_key, parsed=parsed)

    with SessionLocal() as db:
        user_messages = db.scalars(
            select(Message).where(Message.tenant_id == ids["tenant"], Message.sender_type == "USER")
        ).all()
        assert len(user_messages) == 1
        parts = user_messages[0].content_parts or []
        assert [part for part in parts if part.get("type") == "image"]
        event_part = next(part for part in parts if part.get("type") == "webhook_event")
        assert event_part["turn_id"] == "turn_m-1"
        assert event_part["quote"] == "订单 A123"


async def test_turn_with_three_images_processes_all(webhook_media_env):
    service, config, ids, image = webhook_media_env
    media_list = []
    for index, name in enumerate(("a.jpg", "b.jpg", "c.jpg")):
        path = image.parent / name
        path.write_bytes(b"\xff\xd8\xff\xe0" + name.encode())
        media_list.append(_media_item(path, message_id=f"m-{index}"))

    raw = _turn_payload(
        event_id="evt-turn-3",
        text="",
        message_ids=["m-0", "m-1", "m-2"],
        media_list=media_list,
    )

    parsed = service.parse(config, raw, {}, b"{}")
    assert parsed is not None
    assert len(parsed.media) == 3

    await service.process(config, ids["event"], parsed.event_id, parsed.text, parsed.conversation_key, parsed=parsed)

    with SessionLocal() as db:
        message = db.scalar(
            select(Message).where(Message.tenant_id == ids["tenant"], Message.sender_type == "USER")
        )
        assert message is not None
        image_parts = [part for part in (message.content_parts or []) if part.get("type") == "image"]
        assert len(image_parts) == 3


def test_single_text_turn_keeps_old_behavior(webhook_media_env):
    service, config, _ids, _image = webhook_media_env
    raw = _turn_payload(event_id="evt-text-1", text="在吗", message_ids=["m-1"])

    parsed = service.parse(config, raw, {}, b"{}")
    assert parsed is not None
    assert parsed.event_id == "evt-text-1"
    assert parsed.text == "在吗"
    assert parsed.media == ()
    assert parsed.turn_id == "turn_m-1"


def test_two_separate_sends_are_not_merged(webhook_media_env):
    service, config, _ids, _image = webhook_media_env
    first = service.parse(
        config,
        _turn_payload(event_id="evt-a", text="第一条", message_ids=["m-a"]),
        {},
        b"{}",
    )
    second = service.parse(
        config,
        _turn_payload(event_id="evt-b", text="第二条", message_ids=["m-b"]),
        {},
        b"{}",
    )
    assert first is not None and second is not None
    assert first.event_id != second.event_id
    assert first.text == "第一条"
    assert second.text == "第二条"


def test_duplicate_event_id_is_discarded(webhook_media_env):
    service, config, _ids, _image = webhook_media_env
    assert service.claim(config, "evt-duplicate-1") is not None
    assert service.claim(config, "evt-duplicate-1") is None


def test_media_url_preferred_over_local_path(webhook_media_env):
    service, config, _ids, image = webhook_media_env
    media = _media_item(image, message_id="m-1", url="https://example.com/v1/media/abc")
    parsed = service.parse(
        config,
        _turn_payload(event_id="evt-url-1", text="看图", message_ids=["m-1"], media_list=[media]),
        {},
        b"{}",
    )
    assert parsed is not None
    assert parsed.media[0].source == "https://example.com/v1/media/abc"
    assert parsed.media[0].fallback_source == str(image)


def test_media_list_is_parsed_without_media_path(webhook_media_env):
    service, config, _ids, image = webhook_media_env
    media = _media_item(image, message_id="m-1")
    parsed = service.parse(
        replace(config, media_path=None),
        _turn_payload(event_id="evt-list-1", text="看图", message_ids=["m-1"], media_list=[media]),
        {},
        b"{}",
    )
    assert parsed is not None
    assert len(parsed.media) == 1


def test_media_list_falls_back_to_messages_media(webhook_media_env):
    service, config, _ids, image = webhook_media_env
    media = _media_item(image, message_id="m-1")
    raw = _turn_payload(
        event_id="evt-fallback-1",
        text="看图",
        message_ids=["m-1"],
        messages=[{"message_id": "m-1", "text": "看图", "quote": None, "media": [media]}],
    )
    raw["message"]["media"] = None

    parsed = service.parse(config, raw, {}, b"{}")
    assert parsed is not None
    assert len(parsed.media) == 1
    assert parsed.media[0].message_id == "m-1"


async def test_reply_uses_message_context_token(webhook_media_env, monkeypatch):
    service, config, _ids, _image = webhook_media_env
    parsed = service.parse(
        config,
        _turn_payload(event_id="evt-reply-1", text="在吗", message_ids=["m-1"], context_token="ctx-last"),
        {},
        b"{}",
    )
    assert parsed is not None
    captured: dict = {}

    class _Response:
        status_code = 200
        text = ""

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Response()

    monkeypatch.setattr(
        "app.services.inbound_webhook.httpx.AsyncClient",
        lambda **_kwargs: _Client(),
    )
    await service._reply_to_source(
        replace(config, reply_url="http://bridge.local/v1/messages/send"),
        parsed,
        "收到",
    )

    assert captured["json"]["accountId"] == "account-1"
    assert captured["json"]["peerId"] == "peer-1"
    assert captured["json"]["contextToken"] == "ctx-last"
