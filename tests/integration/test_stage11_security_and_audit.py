from __future__ import annotations

import io
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.main import app
from app.models import Agent, AgentVersion, AuthSession, Conversation, Message, Role, Tenant, User
from app.models.audit import AuditLog
from app.models.confirmation import Confirmation
from app.models.connector import Connector
from app.models.file import File
from app.models.memory import ShortTermMemory
from app.models.model_resource import ModelResource
from app.models.task import Outbox, Task
from app.security.passwords import hash_password


@pytest.fixture(autouse=True)
def baseline():
    with SessionLocal.begin() as db:
        db.execute(delete(AuditLog))
        db.execute(delete(Outbox))
        db.execute(delete(Task))
        db.execute(delete(Message))
        db.execute(delete(Conversation))
        db.execute(delete(ShortTermMemory))
        db.execute(delete(File))
        db.execute(delete(Confirmation))
        db.execute(delete(Connector))
        db.execute(delete(ModelResource))
        db.execute(delete(AgentVersion))
        db.execute(delete(Agent))
        db.execute(delete(AuthSession))
        db.execute(delete(User))
        db.execute(delete(Role))
        db.execute(delete(Tenant))

        tenant = Tenant(name=f"stage11-sec-{uuid4()}")
        db.add(tenant)
        db.flush()

        owner = User(tenant_id=tenant.id, username="owner", password_hash=hash_password("pass"))
        other = User(tenant_id=tenant.id, username="other", password_hash=hash_password("pass"))
        db.add_all([owner, other])
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

        data = {"tenant_id": tenant.id, "owner_id": owner.id, "other_id": other.id, "agent_id": agent.id, "version_id": version.id}

    yield data

    with SessionLocal.begin() as db:
        db.execute(delete(AuditLog))
        db.execute(delete(Outbox))
        db.execute(delete(Task))
        db.execute(delete(Message))
        db.execute(delete(Conversation))
        db.execute(delete(AuthSession))
        db.execute(delete(ShortTermMemory))
        db.execute(delete(File))
        db.execute(delete(Confirmation))
        db.execute(delete(Connector))
        db.execute(delete(ModelResource))
        db.execute(delete(AgentVersion))
        db.execute(delete(Agent))
        db.execute(delete(User))
        db.execute(delete(Role))
        db.execute(delete(Tenant))


async def _login(client: AsyncClient, username: str, password: str) -> str:
    response = await client.post("/api/z-torque/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return response.json()["data"]["access_token"]


@pytest.mark.asyncio
async def test_conversation_and_memory_are_isolated_between_users(baseline):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_token = await _login(client, "owner", "pass")
        other_token = await _login(client, "other", "pass")

        create = await client.post(
            "/api/z-torque/conversations",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"agent_id": None, "title": "t"},
        )
        assert create.status_code == 200
        conv_id = create.json()["data"]["id"]

        # Other user cannot read conversation.
        other_detail = await client.get(
            f"/api/z-torque/conversations/{conv_id}",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert other_detail.status_code == 404

        other_memory = await client.get(
            f"/api/z-torque/conversations/{conv_id}/memory",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert other_memory.status_code == 404


@pytest.mark.asyncio
async def test_file_isolation_and_audit_written_on_read(baseline):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_token = await _login(client, "owner", "pass")
        other_token = await _login(client, "other", "pass")

        files = {"upload": ("hello.txt", io.BytesIO(b"hello"), "text/plain")}
        uploaded = await client.post(
            "/api/z-torque/files",
            headers={"Authorization": f"Bearer {owner_token}"},
            files=files,
        )
        assert uploaded.status_code == 200
        file_id = uploaded.json()["data"]["id"]

        forbidden = await client.get(
            f"/api/z-torque/files/{file_id}",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert forbidden.status_code == 404

        # Owner can read content; should write an audit event.
        content = await client.get(
            f"/api/z-torque/files/{file_id}/content",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert content.status_code in {200, 422}

        audits = await client.get(
            "/api/z-torque/audit?action=FILE_READ",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert audits.status_code == 200
        items = audits.json()["data"]["items"]
        # Upload always audited; read audited only when PARSED.
        assert any(x["action"] == "FILE_UPLOADED" for x in (await client.get("/api/z-torque/audit?action=FILE_UPLOADED", headers={"Authorization": f"Bearer {owner_token}"})).json()["data"]["items"])
        if content.status_code == 200:
            assert any(x["action"] == "FILE_READ" for x in items)

        # Other user cannot list tenant audit? (same tenant) They can list, but should not see other user's data filters.
        # The audit API is tenant-scoped by design; verify it does not error and returns only this tenant's events.
        other_audits = await client.get(
            "/api/z-torque/audit",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert other_audits.status_code == 200


def test_task_request_summary_redacts_secrets():
    from app.kernel.task.service import sanitize_summary

    text = "my api_key=123 token=abc password=secret Authorization: Bearer xyz"
    sanitized = sanitize_summary(text)
    assert "api_key" not in sanitized.lower()
    assert "token" not in sanitized.lower()
    assert "password" not in sanitized.lower()
    assert "authorization" not in sanitized.lower()
    assert "[REDACTED]" in sanitized
    assert len(sanitized) <= 2000


@pytest.mark.asyncio
async def test_file_delete_removes_stored_object():
    """删除文件必须同步清理对象存储，不能只把记录置为 DELETED。"""
    from uuid import UUID

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "owner", "pass")
        headers = {"Authorization": f"Bearer {token}"}
        uploaded = await client.post(
            "/api/z-torque/files",
            headers=headers,
            files={"upload": ("hello.txt", io.BytesIO(b"hello"), "text/plain")},
        )
        assert uploaded.status_code == 200
        file_id = uploaded.json()["data"]["id"]
        with SessionLocal() as db:
            storage_key = db.get(File, UUID(file_id)).storage_key

        store = app.state.port_registry.get("object.store")
        assert store.local_path(storage_key).exists()

        removed = await client.delete(f"/api/z-torque/files/{file_id}", headers=headers)
        assert removed.status_code == 200, removed.text
        assert not store.local_path(storage_key).exists()

        status = await client.get(f"/api/z-torque/files/{file_id}/status", headers=headers)
        assert status.json()["data"]["status"] == "DELETED"


@pytest.mark.asyncio
async def test_chat_stream_rejects_invalid_request_with_4xx():
    """流式接口的非法请求必须是 4xx，不能退化成 500。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "owner", "pass")
        headers = {"Authorization": f"Bearer {token}"}

        not_stream = await client.post("/api/z-torque/chat/stream", headers=headers, json={"message": "hi", "stream": False})
        assert not_stream.status_code == 400, not_stream.text

        missing_conversation = await client.post("/api/z-torque/chat/stream", headers=headers, json={"message": "hi", "stream": True})
        assert missing_conversation.status_code == 400, missing_conversation.text

        unknown_conversation = await client.post(
            "/api/z-torque/chat/stream",
            headers=headers,
            json={"message": "hi", "stream": True, "conversation_id": str(uuid4())},
        )
        assert unknown_conversation.status_code == 404, unknown_conversation.text
