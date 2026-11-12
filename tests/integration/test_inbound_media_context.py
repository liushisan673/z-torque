"""入站媒体进入模型上下文：图片 data URI、能力关闭降级、纯文本回归。"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.main import app
from app.models import Agent, AgentVersion, Conversation, File, Message, Tenant, User
from app.security.passwords import hash_password
from app.services.context_builder import build_context


@pytest.fixture()
def media_context_env():
    store = app.state.port_registry.get("object.store")
    key = f"test-image-{uuid4()}"
    store.put(key, io.BytesIO(b"\xff\xd8\xff\xe0fakejpeg"), max_bytes=1024 * 1024)
    with SessionLocal() as db:
        tenant = Tenant(name=f"ctx-{uuid4()}")
        db.add(tenant)
        db.flush()
        owner = User(tenant_id=tenant.id, username=f"ctx-{uuid4()}", password_hash=hash_password("pass"))
        db.add(owner)
        db.flush()
        agent = Agent(tenant_id=tenant.id, owner_user_id=owner.id, name="ctx-agent", source_type="USER", status="ACTIVE")
        db.add(agent)
        db.flush()
        conversation = Conversation(tenant_id=tenant.id, owner_user_id=owner.id, agent_id=agent.id, title="媒体会话", status="ACTIVE")
        db.add(conversation)
        db.flush()
        item = File(
            tenant_id=tenant.id,
            owner_user_id=owner.id,
            conversation_id=conversation.id,
            original_name="photo.jpg",
            media_type="image/jpeg",
            size_bytes=13,
            sha256="0" * 64,
            storage_key=key,
            status="UPLOADED",
        )
        db.add(item)
        db.flush()
        message = Message(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            sender_type="USER",
            content="",
            content_parts=[{
                "type": "image",
                "file_id": str(item.id),
                "file_name": "photo.jpg",
                "bridge_path": "/data/z-torque-media/outbound/photo.jpg",
            }],
            status="COMPLETED",
        )
        db.add(message)
        db.commit()
        ids = {"tenant": tenant.id, "owner": owner.id, "conversation": conversation.id}
    yield ids
    store.delete(key)
    # 只清理本用例租户，避免误删其他用例留下的父行导致 FK 失败
    with SessionLocal.begin() as db:
        for model in (Message, File, Conversation, AgentVersion, Agent, User):
            db.execute(delete(model).where(model.tenant_id == ids["tenant"]))
        db.execute(delete(Tenant).where(Tenant.id == ids["tenant"]))


def _conversation(db, ids):
    return db.get(Conversation, ids["conversation"])


def test_image_injected_as_data_uri_when_vision_enabled(media_context_env):
    with SessionLocal() as db:
        conversation = _conversation(db, media_context_env)
        context = build_context(db, conversation, supports_vision=True)
    user_messages = [item for item in context if item.role == "user"]
    assert user_messages
    parts = user_messages[-1].content_parts or []
    assert parts and parts[0].type == "image_url"
    assert parts[0].image_url and parts[0].image_url.startswith("data:image/jpeg;base64,")


def test_image_degrades_to_text_when_vision_disabled(media_context_env):
    with SessionLocal() as db:
        conversation = _conversation(db, media_context_env)
        context = build_context(db, conversation, supports_vision=False)
    user_messages = [item for item in context if item.role == "user"]
    assert user_messages
    assert user_messages[-1].content_parts is None
    assert "[已接收图片: photo.jpg]" in (user_messages[-1].content or "")


def test_image_injected_on_latest_user_message_only(media_context_env):
    with SessionLocal() as db:
        conversation = _conversation(db, media_context_env)
        # 造一条更早的文本消息；图片必须注入到最新的用户消息，而不是窗口内最早的那条
        db.add(
            Message(
                tenant_id=media_context_env["tenant"],
                conversation_id=conversation.id,
                sender_type="USER",
                content="更早的文本消息",
                content_parts=None,
                status="COMPLETED",
                created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        )
        db.commit()
        context = build_context(db, conversation, supports_vision=True)
    user_messages = [item for item in context if item.role == "user"]
    latest = user_messages[-1]
    parts = latest.content_parts or []
    assert parts and parts[0].type == "image_url"


def test_plain_text_context_has_no_content_parts(media_context_env):
    with SessionLocal() as db:
        conversation = _conversation(db, media_context_env)
        db.add(
            Message(
                tenant_id=media_context_env["tenant"],
                conversation_id=conversation.id,
                sender_type="USER",
                content="纯文本",
                content_parts=None,
                status="COMPLETED",
            )
        )
        db.commit()
        context = build_context(db, conversation, supports_vision=True)
    last_user = [item for item in context if item.role == "user"][-1]
    assert last_user.content == "纯文本"
    assert last_user.content_parts is None


def test_bridge_path_is_injected_for_forwarding(media_context_env):
    with SessionLocal() as db:
        conversation = _conversation(db, media_context_env)
        context = build_context(db, conversation, supports_vision=True)
    last_user = [item for item in context if item.role == "user"][-1]
    assert "bridge_path=/data/z-torque-media/outbound/photo.jpg" in (last_user.content or "")
