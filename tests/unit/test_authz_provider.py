"""authz.provider Port 收口测试（P1-3：PEP 位置不变，策略来源抽象化）。"""

from uuid import uuid4

import pytest
from sqlalchemy import delete, text

from app.builtin_plugins.authz_provider import AuthzProviderPlugin, BuiltinAuthzProvider
from app.builtin_plugins.manifest import PluginSdk
from app.builtin_plugins.plugins import SkillExecutorPlugin
from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.kernel.registry.port_registry import PortRegistry, PortState
from app.kernel.ports.specs import PORT_SPECS
from app.models import Permission, Role, Tenant, User, role_permission, user_role
from app.security.passwords import hash_password


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with SessionLocal.begin() as db:
        # 全局清理会触碰其他用例残留的外键引用行，沿用 test_auth_flow 的 FK-OFF 模式
        db.execute(text("PRAGMA foreign_keys=OFF"))
        db.execute(delete(role_permission))
        db.execute(delete(user_role))
        db.execute(delete(Permission))
        db.execute(delete(Role))
        db.execute(delete(User))
        db.execute(delete(Tenant))
        db.execute(text("PRAGMA foreign_keys=ON"))


def _setup_user(db, *, grant: tuple[str, ...] = ()):
    tenant = Tenant(name=f"authz-{uuid4()}")
    db.add(tenant)
    db.flush()
    user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
    db.add(user)
    db.flush()
    if grant:
        role = Role(tenant_id=tenant.id, name=f"role-{uuid4()}")
        db.add(role)
        db.flush()
        for name in grant:
            permission = Permission(tenant_id=tenant.id, name=name)
            db.add(permission)
        db.flush()
        for name in grant:
            permission = db.query(Permission).filter_by(name=name).one()
            db.execute(role_permission.insert().values(role_id=role.id, permission_id=permission.id))
        db.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
        db.flush()
    return tenant, user


def test_has_all_granted_permissions():
    with SessionLocal() as db:
        _, user = _setup_user(db, grant=("tool.execute", "db.query"))
        assert BuiltinAuthzProvider().has_all(db, user.id, ("tool.execute", "db.query")) is True
        db.rollback()


def test_has_all_missing_permission_denies():
    with SessionLocal() as db:
        _, user = _setup_user(db, grant=("tool.execute",))
        assert BuiltinAuthzProvider().has_all(db, user.id, ("tool.execute", "admin.iam")) is False
        db.rollback()


def test_has_all_unknown_user_denies():
    with SessionLocal() as db:
        _, user = _setup_user(db, grant=("tool.execute",))
        assert BuiltinAuthzProvider().has_all(db, uuid4(), ("tool.execute",)) is False
        db.rollback()


def test_has_all_empty_requirement_passes():
    with SessionLocal() as db:
        _, user = _setup_user(db)
        assert BuiltinAuthzProvider().has_all(db, user.id, ()) is True
        assert BuiltinAuthzProvider().has_all(db, None, ("tool.execute",)) is False
        db.rollback()


def test_authz_plugin_registers_port():
    spec = next(s for s in PORT_SPECS if s.name == "authz.provider")
    registry = PortRegistry((spec,))
    AuthzProviderPlugin().contribute(PluginSdk.of(AuthzProviderPlugin(), registry=registry, objects={}))
    registry.resolve()
    resolution = registry.resolution("authz.provider")
    assert resolution.state == PortState.RESOLVED
    assert resolution.plugin == "authz_provider"
    assert isinstance(resolution.instance, BuiltinAuthzProvider)


def test_skill_executor_plugin_injects_authz_provider():
    """skill_executors 经 SDK 共享对象注入裁决器（装配顺序：authz_provider 在前）。"""
    specs = tuple(s for s in PORT_SPECS if s.name in {"authz.provider", "skill.executor"})
    registry = PortRegistry(specs)
    objects: dict = {}
    AuthzProviderPlugin().contribute(PluginSdk.of(AuthzProviderPlugin(), registry=registry, objects=objects))
    sdk = PluginSdk.of(SkillExecutorPlugin(), registry=registry, objects=objects, engine=None)
    SkillExecutorPlugin().contribute(sdk)
    assert isinstance(sdk.skill_gateway.permission_checker, BuiltinAuthzProvider)


def test_skill_executor_plugin_requires_authz_provider():
    """缺 authz.provider 时装配显式失败（fail fast，不静默降级）。"""
    from app.kernel.registry.port_registry import AssemblyError

    spec = next(s for s in PORT_SPECS if s.name == "skill.executor")
    registry = PortRegistry((spec,))
    sdk = PluginSdk.of(SkillExecutorPlugin(), registry=registry, objects={}, engine=None)
    with pytest.raises(AssemblyError, match="authz.provider"):
        SkillExecutorPlugin().contribute(sdk)
