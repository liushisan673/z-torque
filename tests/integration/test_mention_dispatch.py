"""任务4：@智能体 = 直接调用该智能体执行（站内 A2A）真实入口测试。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AgentVersion,
    AuthSession,
    Conversation,
    Message,
    Task,
    Tenant,
    User,
)
from app.security.passwords import hash_password
from tests.support.db_cleanup import cleanup_session


@pytest.fixture
def mention_env():
    with cleanup_session() as db:
        for model in (Task, Message, Conversation, AuthSession, User, AgentVersion, Agent, Tenant):
            db.execute(delete(model))
        tenant = Tenant(name=f"mention-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username=f"mention-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        target = Agent(tenant_id=tenant.id, owner_user_id=None, name="写作智能体", description="写报告", source_type="OFFICIAL", status="ACTIVE")
        db.add(target)
        db.flush()
        target_version = AgentVersion(tenant_id=tenant.id, agent_id=target.id, version_number=1, status="PUBLISHED", config_schema_version="1", config_snapshot={})
        db.add(target_version)
        db.flush()
        target.current_published_version_id = target_version.id
        parent = Agent(tenant_id=tenant.id, owner_user_id=None, name="系统默认", description="", source_type="OFFICIAL", status="ACTIVE", is_default_parent=True)
        db.add(parent)
        db.flush()
        parent_version = AgentVersion(tenant_id=tenant.id, agent_id=parent.id, version_number=1, status="PUBLISHED", config_schema_version="1", config_snapshot={})
        db.add(parent_version)
        db.flush()
        parent.current_published_version_id = parent_version.id
        data = {
            "username": user.username,
            "target_id": str(target.id),
            "target_version_id": str(target_version.id),
        }
    yield data
    with SessionLocal.begin() as db:
        for model in (Task, Message, Conversation, AuthSession, User, AgentVersion, Agent, Tenant):
            db.execute(delete(model))


async def _headers(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post("/api/z-torque/auth/login", json={"username": username, "password": "pass"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


@pytest.mark.asyncio
async def test_agent_mention_routes_task_to_mentioned_agent(mention_env):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _headers(client, mention_env["username"])
        response = await client.post(
            "/api/z-torque/chat",
            headers={**headers, "Idempotency-Key": "mention-ok"},
            json={
                "agent_id": None,
                "conversation_id": None,
                "message": "@写作智能体 写一份季度报告",
                "content_parts": None,
                "stream": False,
                "mentions": [{"type": "agent", "id": mention_env["target_id"]}],
            },
        )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["response_type"] == "TASK" and data["task_id"]
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.id == data["task_id"]))
        assert task is not None
        assert str(task.agent_version_id) == mention_env["target_version_id"]
        user_message = db.scalar(select(Message).where(Message.id == data["user_message_id"]))
        assert user_message is not None
        assert "@写作智能体" not in user_message.content
        assert "写一份季度报告" in user_message.content


@pytest.mark.asyncio
async def test_unknown_agent_mention_is_rejected(mention_env):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _headers(client, mention_env["username"])
        response = await client.post(
            "/api/z-torque/chat",
            headers={**headers, "Idempotency-Key": "mention-unknown"},
            json={
                "agent_id": None,
                "conversation_id": None,
                "message": "交给未知智能体处理",
                "content_parts": None,
                "stream": False,
                "mentions": [{"type": "agent", "id": str(uuid4())}],
            },
        )
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_unmentioned_message_keeps_default_parent_behavior(mention_env):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _headers(client, mention_env["username"])
        response = await client.post(
            "/api/z-torque/chat",
            headers={**headers, "Idempotency-Key": "mention-none"},
            json={"agent_id": None, "conversation_id": None, "message": "随便聊聊", "content_parts": None, "stream": False},
        )
    assert response.status_code == 200, response.text
    # 模型返回普通文本时，即使 Agent 可用 A2A 工具，也保持普通 CHAT，不创建 Task。
    assert response.json()["data"]["response_type"] == "CHAT"
    assert response.json()["data"]["task_id"] is None


@pytest.mark.asyncio
async def test_generation_preset_actually_changes_model_request(mention_env, monkeypatch):
    from tests.support.fakes import MockModelGateway

    class RecordingGateway(MockModelGateway):
        def __init__(self):
            self.requests = []

        async def generate(self, request):
            self.requests.append(request)
            return await super().generate(request)

    gateway = RecordingGateway()
    monkeypatch.setattr(app.state, "model_gateway", gateway)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _headers(client, mention_env["username"])
        response = await client.post(
            "/api/z-torque/chat",
            headers={**headers, "Idempotency-Key": "preset-deep"},
            json={"agent_id": mention_env["target_id"], "conversation_id": None, "message": "深入分析这个问题", "content_parts": None, "stream": False, "generation_preset": "deep"},
        )
    assert response.status_code == 200, response.text
    assert gateway.requests
    assert gateway.requests[0].temperature == 0.4


@pytest.mark.asyncio
async def test_non_stream_chat_creates_task_only_when_model_requests_a2a_dispatch(mention_env, monkeypatch):
    from app.kernel.model_gateway.contracts import ModelResponse, ModelToolCall

    class ToolCallingGateway:
        default_model = "mock"

        def __init__(self):
            self.requests = []

        async def generate(self, request):
            self.requests.append(request)
            assert any(tool["function"]["name"] == "route_specialist" for tool in request.tools)
            return ModelResponse(
                content=None,
                tool_calls=[
                    ModelToolCall(
                        id="call-a2a-nonstream",
                        name="route_specialist",
                        arguments={
                            "agent_version_id": mention_env["target_version_id"],
                            "task": "写一份季度报告",
                        },
                    )
                ],
                finish_reason="tool_calls",
            )

    gateway = ToolCallingGateway()
    monkeypatch.setattr(app.state, "model_gateway", gateway)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _headers(client, mention_env["username"])
        response = await client.post(
            "/api/z-torque/chat",
            headers={**headers, "Idempotency-Key": "a2a-tool-call"},
            json={
                "agent_id": None,
                "conversation_id": None,
                "message": "请委派给写作智能体完成季度报告",
                "content_parts": None,
                "stream": False,
            },
        )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["response_type"] == "TASK"
    assert data["task_id"]
    assert gateway.requests
    with SessionLocal() as db:
        task = db.get(Task, data["task_id"])
        assert task is not None
        assert task.parent_task_id is None
        assert task.request_summary == "请委派给写作智能体完成季度报告"
