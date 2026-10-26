"""identity.provider Port 收口测试（P1-2：认证语义收口，行为与迁移前 deps.py 一致）。"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.builtin_plugins.identity_provider import BuiltinIdentityProvider, IdentityProviderPlugin
from app.builtin_plugins.manifest import PluginSdk
from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.kernel.ports.specs import PORT_SPECS
from app.kernel.registry.port_registry import PortRegistry, PortState
from app.models import AuthSession, Tenant, User
from app.security.passwords import hash_password
from app.security.tokens import create_access_token
from tests.support.ddl import fk_off, fk_on


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with SessionLocal.begin() as db:
        # 全局清理会触碰其他用例残留的外键引用行，沿用 test_auth_flow 的 FK-OFF 模式
        fk_off(db)
        db.execute(delete(AuthSession))
        db.execute(delete(User))
        db.execute(delete(Tenant))
        fk_on(db)


def _setup_user(db, *, active=True, revoked=False):
    tenant = Tenant(name=f"ident-{uuid4()}")
    db.add(tenant)
    db.flush()
    user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"), is_active=active)
    db.add(user)
    db.flush()
    auth_session = AuthSession(
        tenant_id=tenant.id,
        user_id=user.id,
        refresh_token_hash=f"hash-{uuid4()}",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        revoked_at=datetime.now(timezone.utc) if revoked else None,
    )
    db.add(auth_session)
    db.flush()
    return tenant, user, auth_session


def test_authenticate_valid_token_returns_security_context():
    with SessionLocal() as db:
        tenant, user, session = _setup_user(db)
        token = create_access_token(str(user.id), str(session.id), str(tenant.id))
        ctx = BuiltinIdentityProvider().authenticate(db, token)
        assert ctx.tenant_id == tenant.id
        assert ctx.user_id == user.id
        assert ctx.session_id == session.id


def test_authenticate_rejects_malformed_token():
    with SessionLocal() as db:
        tenant, user, _ = _setup_user(db)
        with pytest.raises(ApiError) as error:
            BuiltinIdentityProvider().authenticate(db, "not-a-jwt")
        assert error.value.code == "AI_AUTH_FAILED"
        assert error.value.status_code == 401
        db.rollback()


def test_authenticate_rejects_unknown_user():
    with SessionLocal() as db:
        tenant, user, session = _setup_user(db)
        token = create_access_token(str(uuid4()), str(session.id), str(tenant.id))
        with pytest.raises(ApiError) as error:
            BuiltinIdentityProvider().authenticate(db, token)
        assert error.value.code == "AI_AUTH_FAILED"
        db.rollback()


def test_authenticate_rejects_inactive_user():
    with SessionLocal() as db:
        tenant, user, session = _setup_user(db, active=False)
        token = create_access_token(str(user.id), str(session.id), str(tenant.id))
        with pytest.raises(ApiError) as error:
            BuiltinIdentityProvider().authenticate(db, token)
        assert error.value.code == "AI_AUTH_FAILED"
        db.rollback()


def test_authenticate_rejects_revoked_session():
    with SessionLocal() as db:
        tenant, user, session = _setup_user(db, revoked=True)
        token = create_access_token(str(user.id), str(session.id), str(tenant.id))
        with pytest.raises(ApiError) as error:
            BuiltinIdentityProvider().authenticate(db, token)
        assert error.value.code == "AI_AUTH_FAILED"
        db.rollback()


def test_authenticate_rejects_token_without_session():
    with SessionLocal() as db:
        tenant, user, _ = _setup_user(db)
        token = create_access_token(str(user.id), str(uuid4()), str(tenant.id))  # sid 指向不存在的会话
        with pytest.raises(ApiError) as error:
            BuiltinIdentityProvider().authenticate(db, token)
        assert error.value.code == "AI_AUTH_FAILED"
        db.rollback()


def test_load_user_returns_user_or_none():
    with SessionLocal() as db:
        tenant, user, _ = _setup_user(db)
        provider = BuiltinIdentityProvider()
        assert provider.load_user(db, user.id) is user
        assert provider.load_user(db, uuid4()) is None
        db.rollback()


def test_plugin_registers_identity_provider_port():
    spec = next(s for s in PORT_SPECS if s.name == "identity.provider")
    registry = PortRegistry((spec,))
    IdentityProviderPlugin().contribute(PluginSdk.of(IdentityProviderPlugin(), registry=registry))
    registry.resolve()
    resolution = registry.resolution("identity.provider")
    assert resolution.state == PortState.RESOLVED
    assert resolution.plugin == "identity_provider"
    assert isinstance(resolution.instance, BuiltinIdentityProvider)
