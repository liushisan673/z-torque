from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.kernel.agent.version_service import archive_check, archive_version
from app.kernel.task.service import create_task
from app.models import Agent, AgentVersion, AuditLog, Task, Tenant, User
from app.security.passwords import hash_password


def _fixture(db):
    tenant = Tenant(name=f"archive-{uuid4()}")
    db.add(tenant)
    db.flush()
    user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
    db.add(user)
    db.flush()
    agent = Agent(tenant_id=tenant.id, owner_user_id=user.id, name="agent", source_type="USER", status="ACTIVE")
    db.add(agent)
    db.flush()

    def _version(number: int, status: str) -> AgentVersion:
        v = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version_number=number,
            status=status,
            config_schema_version="1",
            config_snapshot={"system_prompt": "test"},
        )
        db.add(v)
        db.flush()
        return v

    current = _version(1, "PUBLISHED")
    target = _version(2, "PUBLISHED")
    agent.current_published_version_id = current.id
    db.flush()
    return tenant, user, agent, current, target


def _task(db, tenant, user, agent, version, status: str) -> Task:
    task = create_task(
        db,
        tenant_id=tenant.id,
        owner_user_id=user.id,
        agent_id=agent.id,
        agent_version_id=version.id,
        idempotency_key=str(uuid4()),
        request_summary="archive check",
    )
    task.status = status
    db.flush()
    return task


def test_archive_version_without_task():
    with SessionLocal() as db:
        tenant, user, agent, _current, target = _fixture(db)
        result = archive_version(db, tenant_id=tenant.id, user_id=user.id, version_id=target.id)
        db.flush()
        assert result.status == "ARCHIVED"
        audit = db.scalar(select(AuditLog).where(AuditLog.resource_id == target.id, AuditLog.action == "AGENT_VERSION_ARCHIVED"))
        assert audit is not None
        db.rollback()


def test_archive_blocked_by_running_task():
    with SessionLocal() as db:
        tenant, user, agent, _current, target = _fixture(db)
        _task(db, tenant, user, agent, target, "RUNNING")
        db.flush()
        with pytest.raises(ApiError) as exc:
            archive_version(db, tenant_id=tenant.id, user_id=user.id, version_id=target.id)
        assert exc.value.code == "AI_AGENT_VERSION_IN_USE"
        assert exc.value.status_code == 409
        assert db.get(AgentVersion, target.id).status == "PUBLISHED"
        db.rollback()


def test_archive_allowed_when_task_is_terminal():
    with SessionLocal() as db:
        tenant, user, agent, _current, target = _fixture(db)
        _task(db, tenant, user, agent, target, "SUCCESS")
        assert archive_version(db, tenant_id=tenant.id, user_id=user.id, version_id=target.id).status == "ARCHIVED"
        db.rollback()


def test_archive_blocked_for_current_published_version():
    with SessionLocal() as db:
        tenant, user, _agent, current, _target = _fixture(db)
        with pytest.raises(ApiError) as exc:
            archive_version(db, tenant_id=tenant.id, user_id=user.id, version_id=current.id)
        assert exc.value.code == "AI_AGENT_VERSION_IS_CURRENT"
        db.rollback()


def test_archive_blocked_by_published_delegation_reference():
    with SessionLocal() as db:
        tenant, user, agent, _current, target = _fixture(db)
        other = Agent(tenant_id=tenant.id, owner_user_id=user.id, name="parent", source_type="USER", status="ACTIVE")
        db.add(other)
        db.flush()
        referrer = AgentVersion(
            tenant_id=tenant.id,
            agent_id=other.id,
            version_number=1,
            status="PUBLISHED",
            config_schema_version="1",
            config_snapshot={"system_prompt": "parent"},
            delegation_enabled=True,
            allowed_child_agent_versions=[str(target.id)],
        )
        db.add(referrer)
        db.flush()
        with pytest.raises(ApiError) as exc:
            archive_version(db, tenant_id=tenant.id, user_id=user.id, version_id=target.id)
        assert exc.value.code == "AI_AGENT_VERSION_REFERENCED"
        db.rollback()


def test_draft_delegation_reference_only_warns():
    with SessionLocal() as db:
        tenant, user, agent, _current, target = _fixture(db)
        referrer = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version_number=3,
            status="DRAFT",
            config_schema_version="1",
            config_snapshot={},
            delegation_enabled=True,
            allowed_child_agent_versions=[str(target.id)],
        )
        db.add(referrer)
        db.flush()
        check = archive_check(db, tenant_id=tenant.id, user_id=user.id, version_id=target.id)
        assert check["can_archive"] is True
        assert check["warnings"] and check["warnings"][0]["code"] == "AI_AGENT_VERSION_REFERENCED_BY_DRAFT"
        db.rollback()


def test_archive_check_reports_running_tasks():
    with SessionLocal() as db:
        tenant, user, agent, _current, target = _fixture(db)
        _task(db, tenant, user, agent, target, "QUEUED")
        db.flush()
        check = archive_check(db, tenant_id=tenant.id, user_id=user.id, version_id=target.id)
        assert check["can_archive"] is False
        assert check["running_task_count"] == 1
        assert check["reasons"][0]["code"] == "AI_AGENT_VERSION_IN_USE"
        db.rollback()


def test_archive_is_idempotent():
    with SessionLocal() as db:
        tenant, user, _agent, _current, target = _fixture(db)
        archive_version(db, tenant_id=tenant.id, user_id=user.id, version_id=target.id)
        db.flush()
        again = archive_version(db, tenant_id=tenant.id, user_id=user.id, version_id=target.id)
        assert again.status == "ARCHIVED"
        assert again.id == target.id
        db.rollback()
