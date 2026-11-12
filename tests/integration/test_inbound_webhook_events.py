"""通用入站 Webhook：消息落库后必须通过会话事件总线推送给控制台。"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from sqlalchemy import select

import app.models  # noqa: F401  # 与仓库其它集成用例一致：确保模型注册
from app.db.session import SessionLocal
from app.kernel.model_gateway import ModelResponse
from app.models import (
    Agent,
    AgentVersion,
    InboundWebhook,
    InboundWebhookEvent,
    Message,
    Tenant,
    User,
)
from app.security.passwords import hash_password
from app.services.chat_event_bus import chat_event_bus
from app.services.inbound_webhook import InboundWebhookService, WebhookConfig


class _FakeGateway:
    default_model = "mock"

    async def generate(self, request):
        return ModelResponse(content="这是回调智能体的回答", finish_reason="stop")


async def test_inbound_webhook_publishes_user_and_reply():
    tenant = Tenant(name=f"webhook-events-{uuid4()}")
    with SessionLocal() as db:
        db.add(tenant)
        db.flush()
        owner = User(tenant_id=tenant.id, username=f"webhook-owner-{uuid4()}", password_hash=hash_password("pass"))
        db.add(owner)
        db.flush()
        agent = Agent(tenant_id=tenant.id, owner_user_id=owner.id, name="webhook-agent", source_type="USER", status="ACTIVE")
        db.add(agent)
        db.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version_number=1,
            status="PUBLISHED",
            config_schema_version="1",
            config_snapshot={"system_prompt": "你是回调助手", "model": "mock"},
        )
        db.add(version)
        db.flush()
        agent.current_published_version_id = version.id
        webhook = InboundWebhook(
            tenant_id=tenant.id,
            owner_user_id=owner.id,
            name="测试回调",
            secret_ciphertext="unused",
            target_agent_id=agent.id,
        )
        db.add(webhook)
        db.flush()
        event = InboundWebhookEvent(tenant_id=tenant.id, webhook_id=webhook.id, event_id="evt-publish-1")
        db.add(event)
        db.commit()
        config = WebhookConfig(
            id=webhook.id,
            tenant_id=tenant.id,
            owner_user_id=owner.id,
            name=webhook.name,
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
            target_agent_id=agent.id,
            message_path="text",
            conversation_key_path="conversation_key",
            event_id_path="event_id",
            enabled=True,
        )
        event_row_id = event.id

    service = InboundWebhookService(session_factory=SessionLocal, model_gateway=_FakeGateway())
    queue = chat_event_bus.subscribe()
    try:
        await service.process(config, event_row_id, "evt-publish-1", "回调内容", "conv-1")
        user_event = await asyncio.wait_for(queue.get(), timeout=2)
        reply_event = await asyncio.wait_for(queue.get(), timeout=2)
    finally:
        chat_event_bus.unsubscribe(queue)

    assert user_event.sender_type == "USER"
    assert user_event.content == "回调内容"
    assert reply_event.sender_type == "ASSISTANT"
    assert reply_event.content == "这是回调智能体的回答"
    assert user_event.conversation_id == reply_event.conversation_id
    with SessionLocal() as db:
        rows = db.scalars(select(Message).where(Message.conversation_id == user_event.conversation_id)).all()
        assert {row.sender_type for row in rows} == {"USER", "ASSISTANT"}
