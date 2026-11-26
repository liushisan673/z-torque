"""会话文件变更审查验收：列变更 → 保留 → 真实撤销，审计保持 metadata-only。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.config import get_settings
from app.db.session import SessionLocal
from app.kernel.task.states import TaskStatus
from app.main import app
from app.models import (
    Agent,
    AgentVersion,
    AuditLog,
    AuthSession,
    Conversation,
    PatchArtifact,
    Task,
    Tenant,
    User,
)
from app.models.patch_artifact import PATCH_REVIEW_KEPT, PATCH_REVIEW_UNDONE
from app.security.passwords import hash_password
from tests.support.db_cleanup import cleanup_session


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with cleanup_session() as db:
        for model in (
            PatchArtifact, Task, Conversation, AuditLog, AuthSession,
            AgentVersion, Agent, User, Tenant,
        ):
            db.execute(delete(model))
    for path in Path(get_settings().md_skills_dir).glob("patch-review-*"):
        path.unlink(missing_ok=True)


def _bootstrap():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"review-{uuid4()}")
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
        agent.current_published_version_id = version.id
        conversation = Conversation(tenant_id=tenant.id, owner_user_id=user.id, agent_id=agent.id, title="review")
        db.add(conversation)
        db.flush()
        task = Task(
            tenant_id=tenant.id,
            owner_user_id=user.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            conversation_id=conversation.id,
            idempotency_key=f"review-{uuid4()}",
            request_summary="review patch",
            root_task_id=uuid4(),
            status=TaskStatus.SUCCESS,
        )
        db.add(task)
        db.flush()
        return tenant.id, user.id, conversation.id, task.id


def _username(user_id) -> str:
    with SessionLocal() as db:
        return db.get(User, user_id).username


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _skills_root() -> Path:
    return Path(get_settings().md_skills_dir).resolve()


@pytest.mark.asyncio
async def test_patch_artifact_keep_and_undo_via_real_http_entry():
    _, user_id, conversation_id, task_id = _bootstrap()
    root = _skills_root()
    target = root / f"patch-review-{uuid4().hex}.txt"
    original = "line1\nline2\n"
    target.write_bytes(original.encode("utf-8"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/z-torque/auth/login", json={"username": _username(user_id), "password": "pass"})
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        patched = await client.post(
            "/api/z-torque/workspace/patch",
            headers=headers,
            json={
                "base_dir": str(root),
                "task_id": str(task_id),
                "patches": [{
                    "path": str(target),
                    "old_text": "line2",
                    "new_text": "line2-new",
                    "expected_hash": _sha(original),
                }],
            },
        )
        assert patched.status_code == 200, patched.text
        artifact_id = patched.json()["data"]["files"][0]["artifact_id"]
        assert artifact_id

        listed = await client.get(
            "/api/z-torque/workspace/patch-artifacts",
            headers=headers,
            params={"conversation_id": str(conversation_id)},
        )
        assert listed.status_code == 200, listed.text
        items = listed.json()["data"]
        assert len(items) == 1
        assert items[0]["id"] == artifact_id
        assert items[0]["review_state"] == "PENDING"
        assert items[0]["additions"] == 1 and items[0]["deletions"] == 1
        assert items[0]["can_undo"] is True

        detail = await client.get(f"/api/z-torque/workspace/patch-artifacts/{artifact_id}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["before_text"] == original
        assert detail.json()["data"]["after_text"] == "line1\nline2-new\n"

        kept = await client.post(f"/api/z-torque/workspace/patch-artifacts/{artifact_id}/keep", headers=headers)
        assert kept.status_code == 200, kept.text
        assert kept.json()["data"]["review_state"] == PATCH_REVIEW_KEPT

        undone = await client.post(f"/api/z-torque/workspace/patch-artifacts/{artifact_id}/undo", headers=headers)
        assert undone.status_code == 200, undone.text
        assert undone.json()["data"]["review_state"] == PATCH_REVIEW_UNDONE
        assert undone.json()["data"]["can_undo"] is False
        assert target.read_bytes() == original.encode("utf-8")

        again = await client.post(f"/api/z-torque/workspace/patch-artifacts/{artifact_id}/undo", headers=headers)
        assert again.status_code == 409
        assert again.json()["code"] == "AI_REQUEST_INVALID"

    with SessionLocal() as db:
        assert db.scalar(select(PatchArtifact).where(PatchArtifact.id == artifact_id)) is not None
        assert db.scalar(select(AuditLog).where(AuditLog.action == "PATCH_KEEP")) is not None
        assert db.scalar(select(AuditLog).where(AuditLog.action == "PATCH_UNDO")) is not None
        assert db.scalars(select(PatchArtifact)).all().__len__() == 1
        undo_audit = db.scalar(select(AuditLog).where(AuditLog.action == "PATCH_UNDO"))
        assert "before_text" not in undo_audit.detail and "after_text" not in undo_audit.detail
