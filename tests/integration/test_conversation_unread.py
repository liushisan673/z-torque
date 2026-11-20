"""会话列表置顶与未读角标。

锁定两条行为：
  1. 列表按 last_message_at 倒序（新消息自动置顶），而不是按 created_at；
  2. unread_count = 已读位（last_read_at）之后的消息数，POST /read 后归零，
     当前打开会话的已读回执由前端触发，这里验证接口语义。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.main import app
from app.models import Agent, AgentVersion, Conversation, Message, Role, Tenant, User
from app.security.passwords import hash_password
from tests.support.ddl import fk_off, fk_on

CONVERSATIONS = "/api/z-torque/conversations"


@pytest.fixture(autouse=True)
def baseline():
    with SessionLocal.begin() as db:
        fk_off(db)
        db.execute(delete(Message))
        db.execute(delete(Conversation))
        db.execute(delete(AgentVersion))
        db.execute(delete(Agent))
        db.execute(delete(User))
        db.execute(delete(Role))
        db.execute(delete(Tenant))
        fk_on(db)

        tenant = Tenant(name="conversation-unread")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username="owner", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        agent = Agent(
            tenant_id=tenant.id,
            owner_user_id=None,
            name="Default Parent",
            description="default",
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

        now = datetime.now(timezone.utc)
        # 旧会话：创建早、但最后消息最新 → 排序应排在最前，且已读位之后有 2 条消息
        old = Conversation(
            tenant_id=tenant.id,
            owner_user_id=user.id,
            agent_id=agent.id,
            title="旧会话",
            status="ACTIVE",
            created_at=now - timedelta(hours=2),
            last_message_at=now,
            last_read_at=now - timedelta(minutes=10),
        )
        # 新会话：创建晚、但最后消息更早 → 排在后面，且历史消息都已读
        fresh = Conversation(
            tenant_id=tenant.id,
            owner_user_id=user.id,
            agent_id=agent.id,
            title="新会话",
            status="ACTIVE",
            created_at=now - timedelta(minutes=30),
            last_message_at=now - timedelta(minutes=20),
            last_read_at=now - timedelta(minutes=10),
        )
        db.add_all([old, fresh])
        db.flush()
        db.add_all(
            [
                Message(tenant_id=tenant.id, conversation_id=old.id, sender_type="USER", content="外部回调 1", status="COMPLETED", created_at=now - timedelta(minutes=5)),
                Message(tenant_id=tenant.id, conversation_id=old.id, sender_type="ASSISTANT", content="自动回复", status="COMPLETED", created_at=now - timedelta(minutes=4)),
                Message(tenant_id=tenant.id, conversation_id=fresh.id, sender_type="USER", content="早先已读的消息", status="COMPLETED", created_at=now - timedelta(minutes=20)),
            ]
        )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client: AsyncClient) -> str:
    resp = await client.post("/api/z-torque/auth/login", json={"username": "owner", "password": "pass"})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


@pytest.mark.asyncio
async def test_list_orders_by_last_message_and_reports_unread():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        resp = await client.get(CONVERSATIONS, headers=_auth(token))
        assert resp.status_code == 200, resp.text
        items = resp.json()["data"]

        # 旧会话虽然创建更早，但最后消息最新 → 置顶
        assert [item["title"] for item in items] == ["旧会话", "新会话"]
        assert items[0]["unread_count"] == 2
        assert items[1]["unread_count"] == 0
        assert items[0]["last_read_at"] is not None


@pytest.mark.asyncio
async def test_read_endpoint_clears_unread_and_is_idempotent():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        items = (await client.get(CONVERSATIONS, headers=_auth(token))).json()["data"]
        target = items[0]

        # 详情接口同样返回未读数
        detail = await client.get(f"{CONVERSATIONS}/{target['id']}", headers=_auth(token))
        assert detail.json()["data"]["unread_count"] == 2

        read = await client.post(f"{CONVERSATIONS}/{target['id']}/read", headers=_auth(token))
        assert read.status_code == 200, read.text
        assert read.json()["data"]["unread_count"] == 0

        # 幂等：重复标记已读不报错，未读保持 0
        again = await client.post(f"{CONVERSATIONS}/{target['id']}/read", headers=_auth(token))
        assert again.status_code == 200, again.text

        after = (await client.get(CONVERSATIONS, headers=_auth(token))).json()["data"]
        assert after[0]["unread_count"] == 0
        assert after[0]["last_read_at"] is not None
