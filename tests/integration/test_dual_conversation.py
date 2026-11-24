"""P1b UX 返工任务 3：双会话独立发送的后端契约验收。

证明后端 /chat 与 /messages 都以显式 conversation_id 为边界，
不存在「同一时刻只能有一个活动会话」的隐含假设；前端双栏可安全复用。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AgentVersion,
    AuthSession,
    Conversation,
    Message,
    Role,
    Task,
    TaskEvent,
    Tenant,
    User,
)
from app.security.passwords import hash_password


@pytest.fixture
def two_conversations():
    with SessionLocal.begin() as db:
        for model in (TaskEvent, Task, Message, Conversation, AgentVersion, Agent, AuthSession, User, Role, Tenant):
            db.execute(delete(model))
        tenant = Tenant(name=f"dual-{uuid4()}")
        db.add(tenant)
        db.flush()
        owner = User(
            tenant_id=tenant.id,
            username=f"dual-{uuid4()}",
            password_hash=hash_password("pass"),
        )
        db.add(owner)
        db.flush()
        agent = Agent(
            tenant_id=tenant.id,
            owner_user_id=None,
            name="Default Parent",
            description="dual",
            source_type="OFFICIAL",
            status="ACTIVE",
            is_default_parent=True,
        )
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
        conversations = []
        for title in ("左会话", "右会话"):
            conversation = Conversation(tenant_id=tenant.id, owner_user_id=owner.id, agent_id=agent.id, title=title, status="ACTIVE")
            db.add(conversation)
            db.flush()
            conversations.append(str(conversation.id))
        data = {"username": owner.username, "conversation_ids": conversations}
    yield data
    with SessionLocal.begin() as db:
        for model in (TaskEvent, Task, Message, Conversation, AgentVersion, Agent, AuthSession, User, Role, Tenant):
            db.execute(delete(model))


async def _headers(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post("/api/z-torque/auth/login", json={"username": username, "password": "pass"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


@pytest.mark.asyncio
async def test_two_conversations_send_independently(two_conversations):
    left, right = two_conversations["conversation_ids"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _headers(client, two_conversations["username"])
        first = await client.post(
            "/api/z-torque/chat",
            headers={**headers, "Idempotency-Key": "dual-left"},
            json={"agent_id": None, "conversation_id": left, "message": "左任务标记 ZQ_DUAL_LEFT", "stream": False, "content_parts": None},
        )
        assert first.status_code == 200, first.text
        assert first.json()["data"]["conversation_id"] == left

        second = await client.post(
            "/api/z-torque/chat",
            headers={**headers, "Idempotency-Key": "dual-right"},
            json={"agent_id": None, "conversation_id": right, "message": "右任务标记 ZQ_DUAL_RIGHT", "stream": False, "content_parts": None},
        )
        assert second.status_code == 200, second.text
        assert second.json()["data"]["conversation_id"] == right

        left_messages = await client.get(f"/api/z-torque/conversations/{left}/messages", headers=headers)
        right_messages = await client.get(f"/api/z-torque/conversations/{right}/messages", headers=headers)
    assert left_messages.status_code == 200 and right_messages.status_code == 200
    left_text = str(left_messages.json()["data"])
    right_text = str(right_messages.json()["data"])
    assert "ZQ_DUAL_LEFT" in left_text and "ZQ_DUAL_RIGHT" not in left_text
    assert "ZQ_DUAL_RIGHT" in right_text and "ZQ_DUAL_LEFT" not in right_text


@pytest.mark.asyncio
async def test_unknown_conversation_id_is_not_mixed_into_other_pane(two_conversations):
    left, _ = two_conversations["conversation_ids"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _headers(client, two_conversations["username"])
        response = await client.post(
            "/api/z-torque/chat",
            headers=headers,
            json={"agent_id": None, "conversation_id": str(uuid4()), "message": "不应写入任何会话", "stream": False, "content_parts": None},
        )
        assert response.status_code in (403, 404), response.text
        left_messages = await client.get(f"/api/z-torque/conversations/{left}/messages", headers=headers)
    assert "不应写入任何会话" not in str(left_messages.json()["data"])
