"""阶段13：首次开通向导的编排接口验收。

- 一次提交创建 Secret/Provider/Model/Endpoint/已发布 Agent/Conversation；
- 同名已发布助手重复提交幂等，不重复创建资源；
- 无 model.manage 权限的普通用户被拒绝。
"""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AgentVersion,
    AuditLog,
    Conversation,
    Message,
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
        db.execute(delete(Message))
        db.execute(delete(Conversation))
        db.execute(delete(AgentVersion))
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
    """返回管理员与普通用户的登录凭据（同一租户）。"""
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
        ids = {
            "tenant_id": str(tenant.id),
            "admin": admin.username,
            "member": member.username,
        }
    yield ids


async def _headers(client: AsyncClient, username: str) -> dict[str, str]:
    resp = await client.post("/api/z-torque/auth/login", json={"username": username, "password": "pass"})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


def _payload() -> dict:
    return {
        "vendor": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model_id": "deepseek-chat",
        "api_key": "sk-test-key",
        "assistant_name": "我的助手",
        "system_prompt": "你是一个测试助手。",
    }


@pytest.mark.asyncio
async def test_onboarding_creates_published_agent_and_conversation(users):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _headers(client, users["admin"])
        resp = await client.post("/api/z-torque/onboarding", headers=headers, json=_payload())
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["already_exists"] is False
        assert data["agent_id"] and data["conversation_id"]

        with SessionLocal() as db:
            providers = db.scalars(select(ModelProvider)).all()
            models = db.scalars(select(ModelResource)).all()
            endpoints = db.scalars(select(ModelEndpoint)).all()
            agents = db.scalars(select(Agent)).all()
            conversations = db.scalars(select(Conversation)).all()
            # 系统自带智能体 + 用户创建的助手 = 2 个智能体；会话绑定系统自带智能体
            assert (len(providers), len(models), len(endpoints), len(conversations)) == (1, 1, 1, 1)
            assert len(agents) == 2
            assert providers[0].secret_ref and providers[0].secret_ref.startswith("secret://")
            assert "sk-test-key" not in providers[0].secret_ref
            system_agent = next(a for a in agents if a.is_default_parent)
            user_agent = next(a for a in agents if not a.is_default_parent)
            assert user_agent.current_published_version_id is not None
            assert conversations[0].agent_id == system_agent.id

        # 重复提交幂等：同名已发布助手已存在时直接返回，不重复建资源
        again = await client.post("/api/z-torque/onboarding", headers=headers, json=_payload())
        assert again.status_code == 200
        assert again.json()["data"]["already_exists"] is True
        with SessionLocal() as db:
            assert len(db.scalars(select(Agent)).all()) == 2  # 系统自带 + 用户助手
            assert len(db.scalars(select(ModelProvider)).all()) == 1


@pytest.mark.asyncio
async def test_onboarding_requires_model_manage(users):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _headers(client, users["member"])
        resp = await client.post("/api/z-torque/onboarding", headers=headers, json=_payload())
        assert resp.status_code == 403
