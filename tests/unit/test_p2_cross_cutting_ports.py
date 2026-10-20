"""P2 横切族 Port 收口测试：interceptor / observability.sink / policy.source + 装配终态。"""

import logging
from uuid import uuid4

import pytest
from sqlalchemy import delete, text

from app.assembly import assemble
from app.builtin_plugins.interceptor import InterceptorPlugin
from app.builtin_plugins.manifest import PluginSdk
from app.builtin_plugins.observability_sink import LoggingObservabilitySink, ObservabilitySinkPlugin
from app.builtin_plugins.policy_source import ConfigPolicySource, PolicySourcePlugin
from app.config import get_settings
from app.db.session import SessionLocal, engine
from app.kernel.audit.service import AuditExecutionInterceptor
from app.kernel.registry.port_registry import PortRegistry, PortState
from app.kernel.ports.specs import PORT_SPECS
from app.models import AuditLog, Permission, Role, Tenant, User, role_permission, user_role
from app.security.passwords import hash_password


# ---- interceptor ----


def test_audit_interceptor_writes_audit_log():
    with SessionLocal() as db:
        tenant = Tenant(name=f"p2x-{uuid4()}")
        db.add(tenant)
        db.flush()
        AuditExecutionInterceptor().on_execution(db, tenant_id=tenant.id, actor_user_id=None, action="SKILL_EXECUTED", resource_type="SKILL_VERSION", resource_id=uuid4(), summary="s", detail={"status": "SUCCEEDED"})
        row = db.query(AuditLog).filter(AuditLog.action == "SKILL_EXECUTED", AuditLog.tenant_id == tenant.id).one_or_none()
        assert row is not None and row.detail == {"status": "SUCCEEDED"}
        db.rollback()


def test_interceptor_plugin_registers_port():
    spec = next(s for s in PORT_SPECS if s.name == "interceptor")
    registry = PortRegistry((spec,))
    InterceptorPlugin().contribute(PluginSdk.of(InterceptorPlugin(), registry=registry, objects={}))
    registry.resolve()
    resolution = registry.resolution("interceptor")
    assert resolution.state == PortState.RESOLVED
    assert resolution.plugin == "interceptor"
    assert isinstance(resolution.instance, AuditExecutionInterceptor)


# ---- observability.sink ----


def test_observability_sink_emits_structured_event(caplog):
    with caplog.at_level(logging.INFO, logger="z_torque.observability"):
        LoggingObservabilitySink().emit("task.completed", {"task_id": "t1", "status": "SUCCESS"})
    assert "task.completed" in caplog.text
    assert '"status": "SUCCESS"' in caplog.text or '"status":"SUCCESS"' in caplog.text


def test_observability_plugin_registers_port():
    spec = next(s for s in PORT_SPECS if s.name == "observability.sink")
    registry = PortRegistry((spec,))
    ObservabilitySinkPlugin().contribute(PluginSdk.of(ObservabilitySinkPlugin(), registry=registry))
    registry.resolve()
    assert registry.resolution("observability.sink").state == PortState.RESOLVED


# ---- policy.source ----


@pytest.fixture(autouse=True)
def cleanup_policy():
    yield
    with SessionLocal.begin() as db:
        db.execute(text("PRAGMA foreign_keys=OFF"))
        db.execute(delete(Permission))
        db.execute(delete(user_role))
        db.execute(delete(Role))
        db.execute(delete(User))
        db.execute(delete(Tenant))
        db.execute(text("PRAGMA foreign_keys=ON"))


def test_policy_source_user_permissions_and_config_lists():
    with SessionLocal() as db:
        tenant = Tenant(name=f"p2p-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        permission = Permission(tenant_id=tenant.id, name="skill.execute")
        db.add(permission)
        db.flush()
        role = Role(tenant_id=tenant.id, name="runner")
        db.add(role)
        db.flush()
        db.execute(role_permission.insert().values(role_id=role.id, permission_id=permission.id))
        db.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
        source = ConfigPolicySource()
        assert source.user_permissions(db, user_id=user.id) == {"skill.execute"}
        settings = get_settings()
        assert source.mcp_allowed_hosts() == list(settings.mcp_allowed_hosts)
        assert source.connector_allowed_targets() == list(settings.connector_allowed_targets)
        assert source.model_allowed_names() == list(settings.model_allowed_names)
        db.rollback()


def test_policy_plugin_registers_port():
    spec = next(s for s in PORT_SPECS if s.name == "policy.source")
    registry = PortRegistry((spec,))
    PolicySourcePlugin().contribute(PluginSdk.of(PolicySourcePlugin(), registry=registry))
    registry.resolve()
    assert registry.resolution("policy.source").state == PortState.RESOLVED


# ---- 装配终态：21 Port 收口后全量装配校验 ----


def test_assembly_resolves_all_21_ports():
    result = assemble(get_settings(), engine)
    status = {item["name"]: item for item in result.registry.status()}
    resolved = [name for name, item in status.items() if item["state"] == PortState.RESOLVED]
    unavailable = [name for name, item in status.items() if item["state"] == PortState.UNAVAILABLE]
    assert len(status) == 21
    # 19 个有社区版实现；vector.store / knowledge.provider 契约已定义、无内置实现 → 显式 UNAVAILABLE
    assert set(unavailable) == {"vector.store", "knowledge.provider"}
    assert len(resolved) == 19
