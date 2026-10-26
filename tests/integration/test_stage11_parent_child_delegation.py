from __future__ import annotations

import json
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
from tests.support.ddl import fk_off, fk_on


@pytest.fixture(autouse=True)
def baseline():
    with SessionLocal.begin() as db:
        # 别的用例会留下审计行，其 actor_user_id 仍指向这里的 User；
        # 清理前统一关外键，避免 PG 上因残留引用报 23503
        fk_off(db)
        # 子表→父表顺序：Message.task_id 为 RESTRICT，必须先于 Task 删除
        db.execute(delete(Message))
        db.execute(delete(Conversation))
        db.execute(delete(TaskEvent))
        db.execute(delete(Task))
        db.execute(delete(AgentVersion))
        db.execute(delete(Agent))
        db.execute(delete(AuthSession))
        db.execute(delete(User))
        db.execute(delete(Role))
        db.execute(delete(Tenant))
        fk_on(db)

        tenant = Tenant(name=f"stage11-{uuid4()}")
        db.add(tenant)
        db.flush()

        owner = User(tenant_id=tenant.id, username="owner", password_hash=hash_password("pass"))
        other = User(tenant_id=tenant.id, username="other", password_hash=hash_password("pass"))
        db.add_all([owner, other])
        db.flush()

        # Default parent must be OFFICIAL and have a published version.
        default_parent = Agent(
            tenant_id=tenant.id,
            owner_user_id=None,
            name="Default Parent",
            description="default",
            source_type="OFFICIAL",
            status="ACTIVE",
            is_default_parent=True,
        )
        db.add(default_parent)
        db.flush()
        default_parent_v1 = AgentVersion(
            tenant_id=tenant.id,
            agent_id=default_parent.id,
            version_number=1,
            status="PUBLISHED",
            config_schema_version="1",
            config_snapshot={"mode": "mock"},
        )
        db.add(default_parent_v1)
        db.flush()
        default_parent.current_published_version_id = default_parent_v1.id

        # A secondary official agent (non-default).
        other_official = Agent(
            tenant_id=tenant.id,
            owner_user_id=None,
            name="Official Other",
            description="other",
            source_type="OFFICIAL",
            status="ACTIVE",
            is_default_parent=False,
        )
        db.add(other_official)
        db.flush()
        other_official_v1 = AgentVersion(
            tenant_id=tenant.id,
            agent_id=other_official.id,
            version_number=1,
            status="PUBLISHED",
            config_schema_version="1",
            config_snapshot={"mode": "mock"},
        )
        db.add(other_official_v1)
        db.flush()
        other_official.current_published_version_id = other_official_v1.id

        data = {
            "tenant_id": tenant.id,
            "owner_id": owner.id,
            "other_id": other.id,
            "default_parent_id": default_parent.id,
            "default_parent_v1": default_parent_v1.id,
            "other_official_id": other_official.id,
            "other_official_v1": other_official_v1.id,
        }

    yield data

    with SessionLocal.begin() as db:
        db.execute(delete(TaskEvent))
        db.execute(delete(Task))
        db.execute(delete(Message))
        db.execute(delete(Conversation))
        db.execute(delete(AgentVersion))
        db.execute(delete(Agent))
        db.execute(delete(AuthSession))
        db.execute(delete(User))
        db.execute(delete(Role))
        db.execute(delete(Tenant))


async def _login(client: AsyncClient, username: str, password: str) -> str:
    response = await client.post("/api/z-torque/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return response.json()["data"]["access_token"]


@pytest.mark.asyncio
async def test_default_parent_used_when_agent_not_selected(baseline):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "owner", "pass")
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "stage11-default-parent"}
        response = await client.post(
            "/api/z-torque/chat",
            headers=headers,
            json={"agent_id": None, "conversation_id": None, "message": "hello", "stream": False, "content_parts": None},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["conversation_id"]
        assert data["response_type"] in {"CHAT", "TASK"}

    with SessionLocal() as db:
        conv = db.get(Conversation, data["conversation_id"])
        assert conv is not None
        assert str(conv.agent_id) == str(baseline["default_parent_id"])


@pytest.mark.asyncio
async def test_child_task_requires_delegation_fields_and_depth_is_limited(baseline):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "owner", "pass")
        headers = {"Authorization": f"Bearer {token}"}
        root = await client.post(
            "/api/z-torque/tasks",
            headers=headers,
            json={
                "agent_id": str(baseline["default_parent_id"]),
                "agent_version_id": str(baseline["default_parent_v1"]),
                "idempotency_key": "stage11-root",
                "request_summary": "root",
            },
        )
        assert root.status_code == 200
        root_id = root.json()["data"]["id"]

        # Backward compatible behavior: when parent_task_id is provided,
        # delegation_id and child_sequence are auto-filled if omitted.
        missing = await client.post(
            "/api/z-torque/tasks",
            headers=headers,
            json={
                "agent_id": str(baseline["default_parent_id"]),
                "agent_version_id": str(baseline["default_parent_v1"]),
                "idempotency_key": "stage11-child-missing",
                "request_summary": "child",
                "parent_task_id": root_id,
            },
        )
        assert missing.status_code == 200
        assert missing.json()["data"]["delegation_depth"] == 1

        child = await client.post(
            "/api/z-torque/tasks",
            headers=headers,
            json={
                "agent_id": str(baseline["default_parent_id"]),
                "agent_version_id": str(baseline["default_parent_v1"]),
                "idempotency_key": "stage11-child",
                "request_summary": "child",
                "parent_task_id": root_id,
                "delegation_id": str(uuid4()),
                "child_sequence": 2,
            },
        )
        assert child.status_code == 200
        child_id = child.json()["data"]["id"]

        grandchild = await client.post(
            "/api/z-torque/tasks",
            headers=headers,
            json={
                "agent_id": str(baseline["default_parent_id"]),
                "agent_version_id": str(baseline["default_parent_v1"]),
                "idempotency_key": "stage11-grandchild",
                "request_summary": "grandchild",
                "parent_task_id": child_id,
                "delegation_id": str(uuid4()),
                "child_sequence": 1,
            },
        )
        assert grandchild.status_code == 409
        assert grandchild.json()["code"] == "AI_AGENT_DELEGATION_DEPTH_EXCEEDED"


@pytest.mark.asyncio
async def test_duplicate_delegation_id_is_rejected(baseline):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "owner", "pass")
        headers = {"Authorization": f"Bearer {token}"}
        root = await client.post(
            "/api/z-torque/tasks",
            headers=headers,
            json={
                "agent_id": str(baseline["default_parent_id"]),
                "agent_version_id": str(baseline["default_parent_v1"]),
                "idempotency_key": "stage11-root-dup",
                "request_summary": "root",
            },
        )
        root_id = root.json()["data"]["id"]
        delegation_id = str(uuid4())

        first = await client.post(
            "/api/z-torque/tasks",
            headers=headers,
            json={
                "agent_id": str(baseline["default_parent_id"]),
                "agent_version_id": str(baseline["default_parent_v1"]),
                "idempotency_key": "stage11-child-1",
                "request_summary": "child",
                "parent_task_id": root_id,
                "delegation_id": delegation_id,
                "child_sequence": 1,
            },
        )
        assert first.status_code == 200

        second = await client.post(
            "/api/z-torque/tasks",
            headers=headers,
            json={
                "agent_id": str(baseline["default_parent_id"]),
                "agent_version_id": str(baseline["default_parent_v1"]),
                "idempotency_key": "stage11-child-2",
                "request_summary": "child-dup",
                "parent_task_id": root_id,
                "delegation_id": delegation_id,
                "child_sequence": 2,
            },
        )
        assert second.status_code == 409
        assert second.json()["code"] == "AI_TASK_DUPLICATE_IDEMPOTENCY"


def _parse_sse_events(raw: str) -> list[tuple[str | None, str | None, dict]]:
    events: list[tuple[str | None, str | None, dict]] = []
    buf: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip():
            if "data" in buf:
                payload = json.loads(buf["data"])
                events.append((buf.get("id"), buf.get("event"), payload))
            buf = {}
            continue
        if line.startswith("id:"):
            buf["id"] = line[3:].strip()
        elif line.startswith("event:"):
            buf["event"] = line[6:].strip()
        elif line.startswith("data:"):
            buf["data"] = line[5:].strip()
    return events


@pytest.mark.asyncio
async def test_sse_snapshot_includes_parent_child_fields_and_isolation(baseline, monkeypatch):
    # 任务无 worker 会保持 QUEUED，长连接需等满 _MAX_STREAM_SECONDS 才断开；
    # ASGITransport 会缓冲整个响应体，客户端无法提前中断，故把上限压到 1s
    monkeypatch.setattr("app.services.task_stream._MAX_STREAM_SECONDS", 1.0)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_token = await _login(client, "owner", "pass")
        other_token = await _login(client, "other", "pass")

        owner_headers = {"Authorization": f"Bearer {owner_token}"}
        other_headers = {"Authorization": f"Bearer {other_token}"}

        root = await client.post(
            "/api/z-torque/tasks",
            headers=owner_headers,
            json={
                "agent_id": str(baseline["default_parent_id"]),
                "agent_version_id": str(baseline["default_parent_v1"]),
                "idempotency_key": "stage11-sse-root",
                "request_summary": "root",
            },
        )
        root_id = root.json()["data"]["id"]

        child = await client.post(
            "/api/z-torque/tasks",
            headers=owner_headers,
            json={
                "agent_id": str(baseline["default_parent_id"]),
                "agent_version_id": str(baseline["default_parent_v1"]),
                "idempotency_key": "stage11-sse-child",
                "request_summary": "child",
                "parent_task_id": root_id,
                "delegation_id": str(uuid4()),
                "child_sequence": 1,
            },
        )
        child_id = child.json()["data"]["id"]

        forbidden_task = await client.get(f"/api/z-torque/tasks/{child_id}", headers=other_headers)
        assert forbidden_task.status_code == 404

        forbidden_stream = await client.get(f"/api/z-torque/tasks/{child_id}/stream", headers=other_headers)
        assert forbidden_stream.status_code == 404

        stream = await client.get(f"/api/z-torque/tasks/{child_id}/stream", headers=owner_headers)
        assert stream.status_code == 200
        events = _parse_sse_events(stream.text)
        assert any(e[1] == "task.snapshot" for e in events)
        snapshot = next(e for e in events if e[1] == "task.snapshot")[2]
        assert snapshot["parent_task_id"] == root_id
        assert snapshot["root_task_id"]
        assert snapshot["delegation_depth"] == 1
        assert snapshot["child_sequence"] == 1
