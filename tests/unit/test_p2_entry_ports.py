"""P2 入口族 Port 收口测试：notify.channel / approval.provider（包装式，行为与收口前一致）。"""

from uuid import uuid4

import pytest
from sqlalchemy import delete, text

from app.builtin_plugins.approval_provider import ApprovalProviderPlugin
from app.builtin_plugins.manifest import PluginSdk
from app.builtin_plugins.notify_channel import LoggingNotifyChannel, NotifyChannelPlugin
from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.kernel.registry.port_registry import PortRegistry, PortState
from app.kernel.ports.specs import PORT_SPECS
from app.kernel.skill_confirmation import BuiltinApprovalProvider
from app.models import Confirmation, Tenant, User
from app.security.passwords import hash_password


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with SessionLocal.begin() as db:
        db.execute(text("PRAGMA foreign_keys=OFF"))
        db.execute(delete(Confirmation))
        db.execute(delete(User))
        db.execute(delete(Tenant))
        db.execute(text("PRAGMA foreign_keys=ON"))


def _setup_user(db):
    tenant = Tenant(name=f"p2-{uuid4()}")
    db.add(tenant)
    db.flush()
    user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
    db.add(user)
    db.flush()
    return tenant, user


# ---- notify.channel ----


def test_notify_channel_logs_without_error(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="z_torque.notify"):
        LoggingNotifyChannel().send(tenant_id=uuid4(), user_id=None, title="任务完成", body="hello", level="warning", meta={"task_id": "t1"})
    assert "任务完成" in caplog.text


def test_plugin_registers_notify_channel_port():
    spec = next(s for s in PORT_SPECS if s.name == "notify.channel")
    registry = PortRegistry((spec,))
    NotifyChannelPlugin().contribute(PluginSdk.of(NotifyChannelPlugin(), registry=registry))
    registry.resolve()
    resolution = registry.resolution("notify.channel")
    assert resolution.state == PortState.RESOLVED
    assert resolution.plugin == "notify_channel"
    assert isinstance(resolution.instance, LoggingNotifyChannel)


# ---- approval.provider ----


def test_approval_request_creates_pending_confirmation_with_audit():
    from app.models import AuditLog

    with SessionLocal() as db:
        tenant, user = _setup_user(db)
        provider = BuiltinApprovalProvider()
        confirmation = provider.request(db, tenant_id=tenant.id, requested_by_user_id=user.id, reason="敏感操作", snapshot={"k": "v"})
        row = db.get(Confirmation, confirmation.id)
        assert row is not None and row.status == "PENDING"
        audit = db.query(AuditLog).filter(AuditLog.action == "CONFIRMATION_REQUESTED", AuditLog.resource_id == confirmation.id).one_or_none()
        assert audit is not None
        db.rollback()


def test_approval_resolve_approve_and_idempotent():
    with SessionLocal() as db:
        tenant, requester = _setup_user(db)
        approver = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
        db.add(approver)
        db.flush()
        provider = BuiltinApprovalProvider()
        confirmation = provider.request(db, tenant_id=tenant.id, requested_by_user_id=requester.id, reason="r", snapshot={})
        db.commit()
        resolved = provider.resolve(db, tenant_id=tenant.id, confirmation_id=confirmation.id, user_id=approver.id, approved=True)
        assert resolved.status == "APPROVED"
        # 幂等：同方向重复裁决返回既有结果
        again = provider.resolve(db, tenant_id=tenant.id, confirmation_id=confirmation.id, user_id=approver.id, approved=True)
        assert again.status == "APPROVED"
        db.rollback()


def test_approval_resolve_rejects_self_approval():
    with SessionLocal() as db:
        tenant, user = _setup_user(db)
        provider = BuiltinApprovalProvider()
        confirmation = provider.request(db, tenant_id=tenant.id, requested_by_user_id=user.id, reason="r", snapshot={})
        db.commit()
        with pytest.raises(ApiError) as error:
            provider.resolve(db, tenant_id=tenant.id, confirmation_id=confirmation.id, user_id=user.id, approved=True)
        assert error.value.code == "AI_CONFIRMATION_SELF_APPROVAL"
        db.rollback()


def test_plugin_registers_approval_provider_port():
    spec = next(s for s in PORT_SPECS if s.name == "approval.provider")
    registry = PortRegistry((spec,))
    ApprovalProviderPlugin().contribute(PluginSdk.of(ApprovalProviderPlugin(), registry=registry, objects={}))
    registry.resolve()
    resolution = registry.resolution("approval.provider")
    assert resolution.state == PortState.RESOLVED
    assert resolution.plugin == "approval_provider"
    assert isinstance(resolution.instance, BuiltinApprovalProvider)
