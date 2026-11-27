"""首次配置：只更新内置 Provider 的 API Key，不改动模型与智能体。"""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AuditLog,
    Conversation,
    ModelEndpoint,
    ModelProvider,
    ModelResource,
    Permission,
    Role,
    Tenant,
    User,
    role_permission,
    user_role,
)
from app.security.passwords import hash_password
from tests.support.db_cleanup import cleanup_session


@pytest.fixture(autouse=True)
def _clean():
    with cleanup_session() as db:
        db.execute(delete(Conversation))
        db.execute(delete(Agent))
        db.execute(delete(ModelEndpoint))
        db.execute(delete(ModelResource))
        db.execute(delete(ModelProvider))
        db.execute(delete(AuditLog))
        db.execute(delete(user_role))
        db.execute(delete(role_permission))
        db.execute(delete(Role))
        db.execute(delete(Permission))
        db.execute(delete(User))
        db.execute(delete(Tenant))
    yield


@pytest.fixture()
def users():
    """返回管理员与普通用户凭据，并预置一个与种子包等价的内置 Provider。"""
    suffix = uuid4().hex[:8]
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"onboarding-{suffix}")
        db.add(tenant)
        db.flush()
        role = Role(tenant_id=tenant.id, name="admin")
        permission = Permission(tenant_id=tenant.id, name="model.manage")
        db.add_all([role, permission])
        db.flush()
        db.execute(role_permission.insert().values(role_id=role.id, permission_id=permission.id))
        admin = User(tenant_id=tenant.id, username=f"admin-{suffix}", password_hash=hash_password("pass"))
        member = User(tenant_id=tenant.id, username=f"member-{suffix}", password_hash=hash_password("pass"))
        db.add_all([admin, member])
        db.flush()
        db.execute(user_role.insert().values(user_id=admin.id, role_id=role.id))
        provider = ModelProvider(
            tenant_id=tenant.id,
            created_by=admin.id,
            name="DeepSeek",
            provider_type="openai_compatible",
            status="ENABLED",
            base_url="https://api.deepseek.com/v1",
            secret_ref=None,
            config={},
        )
        db.add(provider)
        db.flush()
        ids = {
            "tenant_id": str(tenant.id),
            "admin": admin.username,
            "member": member.username,
            "provider_id": str(provider.id),
        }
    yield ids


async def _headers(client: AsyncClient, username: str) -> dict[str, str]:
    resp = await client.post("/api/z-torque/auth/login", json={"username": username, "password": "pass"})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


@pytest.mark.asyncio
async def test_onboarding_only_sets_bundled_api_key(users):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _headers(client, users["admin"])
        resp = await client.post(
            "/api/z-torque/onboarding",
            headers=headers,
            json={"api_key": "sk-test-key", "model_id": "other-model", "base_url": "https://evil.example.com"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["configured"] is True
        assert data["already_configured"] is False

        with SessionLocal() as db:
            providers = db.scalars(select(ModelProvider)).all()
            assert len(providers) == 1
            provider = providers[0]
            assert provider.secret_ref and provider.secret_ref.startswith("secret://")
            assert "sk-test-key" not in provider.secret_ref
            # 模型标识与接入地址保持内置值，用户传的其它字段被忽略
            assert provider.base_url == "https://api.deepseek.com/v1"
            assert db.scalars(select(ModelResource)).all() == []
            assert db.scalars(select(ModelEndpoint)).all() == []
            assert db.scalars(select(Agent)).all() == []
            assert db.scalars(select(Conversation)).all() == []

        status = await client.get("/api/z-torque/onboarding/status", headers=headers)
        assert status.json()["data"] == {"configured": True, "needed": False}

        # 再次提交只更新密钥，不创建任何资源
        again = await client.post(
            "/api/z-torque/onboarding", headers=headers, json={"api_key": "sk-test-key-2"}
        )
        assert again.status_code == 200
        assert again.json()["data"]["already_configured"] is True
        with SessionLocal() as db:
            assert len(db.scalars(select(ModelProvider)).all()) == 1
            assert db.scalars(select(ModelResource)).all() == []
            assert db.scalars(select(Agent)).all() == []


@pytest.mark.asyncio
async def test_onboarding_requires_model_manage(users):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _headers(client, users["member"])
        resp = await client.post("/api/z-torque/onboarding", headers=headers, json={"api_key": "sk-test-key"})
        assert resp.status_code == 403
