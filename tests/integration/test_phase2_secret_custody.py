"""Phase 2 阶段六：Channels 凭据托管与审计真实入口验收。

负向先行：空引用 / 非法密文必须显式失败且不回显原文；正向验证集中解析、
convert 审计 metadata-only、渠道凭据审计事件登记与分类。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.errors import SECRET_ERROR_CODES, ApiError
from app.db.session import SessionLocal
from app.kernel.audit.categories import CATEGORY_CREDENTIAL, audit_category
from app.main import app
from app.models import (
    AuditLog,
    Permission,
    Role,
    Tenant,
    User,
    role_permission,
    user_role,
)
from app.security.passwords import hash_password
from app.security.secret_resolver import is_secret_ref, resolve_secret, secret_ref_of
from app.security.secret_store import SecretStore

_CREATED_TENANTS: list = []


@pytest.fixture(autouse=True)
def cleanup():
    _CREATED_TENANTS.clear()
    yield
    with SessionLocal.begin() as db:
        if _CREATED_TENANTS:
            role_ids = select(Role.id).where(Role.tenant_id.in_(_CREATED_TENANTS))
            db.execute(delete(user_role).where(user_role.c.user_id.in_(
                select(User.id).where(User.tenant_id.in_(_CREATED_TENANTS))
            )))
            db.execute(delete(role_permission).where(role_permission.c.role_id.in_(role_ids)))
            db.execute(delete(AuditLog).where(AuditLog.tenant_id.in_(_CREATED_TENANTS)))
            db.execute(delete(Role).where(Role.tenant_id.in_(_CREATED_TENANTS)))
            db.execute(delete(Permission).where(Permission.tenant_id.in_(_CREATED_TENANTS)))
            db.execute(delete(User).where(User.tenant_id.in_(_CREATED_TENANTS)))
            db.execute(delete(Tenant).where(Tenant.id.in_(_CREATED_TENANTS)))
    _CREATED_TENANTS.clear()


def _bootstrap_manage_user():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"secret-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(
            tenant_id=tenant.id, username=f"mgr-{uuid4()}",
            password_hash=hash_password("pass"),
        )
        db.add(user)
        db.flush()
        role = Role(tenant_id=tenant.id, name="connector_manager")
        db.add(role)
        db.flush()
        permission = Permission(tenant_id=tenant.id, name="connector.manage")
        db.add(permission)
        db.flush()
        db.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
        db.execute(role_permission.insert().values(role_id=role.id, permission_id=permission.id))
        _CREATED_TENANTS.append(tenant.id)
        return tenant.id, user.username


def test_resolver_rejects_empty_and_invalid_references():
    """负向：空引用不退化为空明文；非法密文显式失败且不回显内容。"""
    provider = SecretStore()
    with pytest.raises(ApiError) as exc:
        resolve_secret("secret://", provider)
    assert exc.value.code == "AI_SECRET_INVALID_REF"
    with pytest.raises(ApiError) as exc:
        resolve_secret("secret://not-a-valid-fernet-token", provider, channel="webhook")
    assert exc.value.code == "AI_SECRET_INVALID_REF"
    assert "not-a-valid" not in exc.value.message
    with pytest.raises(ApiError) as exc:
        resolve_secret("", provider, required=True)
    assert exc.value.code in SECRET_ERROR_CODES


def test_resolver_roundtrip_and_plaintext_compat():
    """正向：secret:// 解密，非引用明文原样返回。"""
    provider = SecretStore()
    encrypted = provider.encrypt("s3cr3t-value")
    assert is_secret_ref(secret_ref_of(encrypted)) is True
    assert resolve_secret(secret_ref_of(encrypted), provider) == "s3cr3t-value"
    assert resolve_secret("plain-value", provider) == "plain-value"


def test_credential_audit_events_are_registered_and_categorized():
    for action in (
        "SECRET_CONVERTED",
        "CHANNEL_CREDENTIAL_STORED",
        "CHANNEL_CREDENTIAL_ROTATED",
        "CHANNEL_CREDENTIAL_RESOLVE_FAILED",
    ):
        assert audit_category(action) == CATEGORY_CREDENTIAL


@pytest.mark.asyncio
async def test_convert_writes_metadata_only_audit():
    tenant_id, username = _bootstrap_manage_user()
    plain = "sk-should-never-appear-in-audit"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(
            "/api/z-torque/auth/login", json={"username": username, "password": "pass"}
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        response = await client.post(
            "/api/z-torque/secrets/convert", json={"value": plain}, headers=headers
        )
    assert response.status_code == 200, response.text
    secret_ref = response.json()["data"]["secret_ref"]
    assert secret_ref.startswith("secret://")

    with SessionLocal() as db:
        row = db.scalar(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action == "SECRET_CONVERTED",
            )
        )
    assert row is not None
    detail = row.detail or {}
    assert set(detail) == {"ref_hash", "value_length", "channel"}
    assert detail["channel"] == "manual"
    assert detail["value_length"] == len(plain)
    assert plain not in str(detail)
    assert secret_ref not in str(detail)
