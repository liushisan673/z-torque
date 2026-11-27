"""ChangeSet 提案验收：先提案后落地，Keep/Cancel/回退均需显式确认。"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AgentVersion,
    AuditLog,
    AuthSession,
    Conversation,
    File,
    FileVersionChange,
    Message,
    Tenant,
    User,
)
from app.models.file_version_change import (
    FILE_CHANGE_ROLLBACK,
    FILE_REVIEW_CANCELLED,
    FILE_REVIEW_KEPT,
)
from app.security.passwords import hash_password
from tests.support.db_cleanup import cleanup_session


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with cleanup_session() as db:
        for model in (
            FileVersionChange, File, Message, Conversation, AuditLog, AuthSession,
            AgentVersion, Agent, User, Tenant,
        ):
            db.execute(delete(model))


def _bootstrap():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"file-review-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        agent = Agent(tenant_id=tenant.id, owner_user_id=None, name="A", source_type="OFFICIAL", status="ACTIVE")
        db.add(agent)
        db.flush()
        version = AgentVersion(
            tenant_id=tenant.id, agent_id=agent.id, version_number=1, status="PUBLISHED",
            config_schema_version="1", config_snapshot={"mode": "mock"},
        )
        db.add(version)
        db.flush()
        conversation = Conversation(tenant_id=tenant.id, owner_user_id=user.id, agent_id=agent.id, title="file-review")
        db.add(conversation)
        db.flush()
        return tenant.id, user.id, conversation.id


def _username(user_id) -> str:
    with SessionLocal() as db:
        return db.get(User, user_id).username


async def _export(client, headers, conversation_id, content, filename="方案.docx"):
    response = await client.post(
        "/api/z-torque/chat/export-document",
        headers=headers,
        json={"conversation_id": str(conversation_id), "format": "docx", "filename": filename, "content": content},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.asyncio
async def test_file_change_proposal_keep_cancel_and_rollback():
    _, user_id, conversation_id = _bootstrap()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/z-torque/auth/login", json={"username": _username(user_id), "password": "pass"})
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

        first = await _export(client, headers, conversation_id, "第一段\n第二段\n")
        assert first["review_state"] == "PENDING"
        assert first["file_id"] is None and first["download_url"] is None
        with SessionLocal() as db:
            assert db.scalar(select(File).where(File.original_name == "方案.docx")) is None

        listed = await client.get(
            "/api/z-torque/workspace/file-changes", headers=headers,
            params={"conversation_id": str(conversation_id)},
        )
        assert listed.status_code == 200, listed.text
        assert len(listed.json()["data"]) == 1
        assert listed.json()["data"][0]["can_keep"] is True
        assert listed.json()["data"][0]["kind"] == "CREATE"

        kept = await client.post(f"/api/z-torque/workspace/file-changes/{first['version_change_id']}/keep", headers=headers)
        assert kept.status_code == 200, kept.text
        assert kept.json()["data"]["review_state"] == FILE_REVIEW_KEPT
        with SessionLocal() as db:
            first_change = db.get(FileVersionChange, UUID(first["version_change_id"]))
            first_file = db.get(File, first_change.current_file_id)
            assert first_file is not None and first_file.status == "PARSED"
            first_file_id = first_file.id

        second = await _export(client, headers, conversation_id, "第一段\n第二段-优化\n")
        second_id = second["version_change_id"]
        with SessionLocal() as db:
            second_change = db.get(FileVersionChange, UUID(second_id))
            assert second_change.previous_file_id == first_file_id
            assert second_change.before_text == "第一段\n第二段\n"
            assert second_change.after_text == "第一段\n第二段-优化\n"

        await client.post(f"/api/z-torque/workspace/file-changes/{second_id}/keep", headers=headers)
        with SessionLocal() as db:
            second_change = db.get(FileVersionChange, UUID(second_id))
            second_file = db.get(File, second_change.current_file_id)
            assert second_file is not None and second_file.status == "PARSED"
            second_file_id = second_file.id

        third = await _export(client, headers, conversation_id, "第一段\n第三段\n")
        third_id = third["version_change_id"]
        cancelled = await client.post(f"/api/z-torque/workspace/file-changes/{third_id}/cancel", headers=headers)
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["data"]["review_state"] == FILE_REVIEW_CANCELLED
        with SessionLocal() as db:
            assert db.scalar(select(File).where(File.id == db.get(FileVersionChange, UUID(third_id)).current_file_id)) is None

        rollback = await client.post(f"/api/z-torque/workspace/file-changes/{second_id}/undo", headers=headers)
        assert rollback.status_code == 200, rollback.text
        rollback_proposal = rollback.json()["data"]
        assert rollback_proposal["review_state"] == "PENDING"
        assert rollback_proposal["kind"] == FILE_CHANGE_ROLLBACK
        await client.post(f"/api/z-torque/workspace/file-changes/{rollback_proposal['id']}/keep", headers=headers)

        with SessionLocal() as db:
            assert db.get(File, second_file_id).status == "DELETED"
            assert db.get(File, first_file_id).status == "PARSED"
            assert db.scalar(select(AuditLog).where(AuditLog.action == "FILE_CHANGE_PROPOSED")) is not None
            assert db.scalar(select(AuditLog).where(AuditLog.action == "FILE_CHANGE_CANCELLED")) is not None


@pytest.mark.asyncio
async def test_file_change_download_pending_draft_and_kept_file():
    _, user_id, conversation_id = _bootstrap()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/z-torque/auth/login", json={"username": _username(user_id), "password": "pass"})
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

        exported = await _export(client, headers, conversation_id, "我的妈妈\n")
        change_id = exported["version_change_id"]

        listed = await client.get(
            "/api/z-torque/workspace/file-changes", headers=headers,
            params={"conversation_id": str(conversation_id)},
        )
        pending = listed.json()["data"][0]
        assert pending["file_id"] is None
        assert pending["download_url"] == f"/api/z-torque/workspace/file-changes/{change_id}/download"

        draft = await client.get(pending["download_url"], headers=headers)
        assert draft.status_code == 200, draft.text
        assert draft.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert draft.content[:2] == b"PK"
        assert "attachment" in draft.headers["content-disposition"]
        with SessionLocal() as db:
            assert db.get(FileVersionChange, UUID(change_id)).review_state == "PENDING"

        await client.post(f"/api/z-torque/workspace/file-changes/{change_id}/keep", headers=headers)
        kept_list = await client.get(
            "/api/z-torque/workspace/file-changes", headers=headers,
            params={"conversation_id": str(conversation_id)},
        )
        kept = kept_list.json()["data"][0]
        assert kept["file_id"] is not None
        assert kept["download_url"] == f"/api/z-torque/files/{kept['file_id']}/raw"
        downloaded = await client.get(kept["download_url"], headers=headers)
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content[:2] == b"PK"
