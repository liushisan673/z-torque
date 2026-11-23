"""阶段 12 M12-2：一键保存并发布。

- B-1 技能发布原子化：publish 请求体可携带未保存修改（指令/确认开关/绑定），先落库再发布，同一事务；
- B-2 智能体创建即发布：创建智能体 + 首版本 + 绑定技能 + 发布一步完成；技能版本未发布返回明确错误码。
"""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AgentVersion,
    AgentVersionSkill,
    AuditLog,
    Skill,
    SkillVersion,
    SkillVersionTool,
    Tool,
    ToolVersion,
    User,
)
from app.security.passwords import hash_password


@pytest.fixture(autouse=True)
def _clean():
    with SessionLocal.begin() as db:
        db.execute(delete(AuditLog))
        db.execute(delete(SkillVersionTool))
        db.execute(delete(AgentVersionSkill))
        db.execute(delete(SkillVersion))
        db.execute(delete(ToolVersion))
        db.execute(delete(Tool))
        db.execute(delete(Skill))
        db.execute(delete(AgentVersion))
        db.execute(delete(Agent))
        db.execute(delete(User))
    yield


@pytest.fixture()
def tenant_user():
    from app.models import Tenant

    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"p12-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username="owner", password_hash=hash_password("pass"))
        db.add(user)
        ids = {"tenant_id": str(tenant.id), "user_id": str(user.id)}
    yield ids


async def _login(client: AsyncClient) -> dict[str, str]:
    login = await client.post("/api/z-torque/auth/login", json={"username": "owner", "password": "pass"})
    return {"Authorization": f"Bearer {login.json()['data']['access_token']}"}


async def _create_published_tool(client: AsyncClient, headers: dict[str, str]) -> str:
    tool = await client.post(
        "/api/z-torque/skills-tools/tools",
        headers=headers,
        json={"name": f"tool-{uuid4().hex[:8]}", "description": "t", "tool_type": "HTTP"},
    )
    assert tool.status_code == 200
    tool_id = tool.json()["data"]["id"]
    version = await client.post(
        f"/api/z-torque/skills-tools/tools/{tool_id}/versions",
        headers=headers,
        json={"config_snapshot": {"url": "https://example.com"}, "input_schema": {"type": "object"}},
    )
    assert version.status_code == 200
    tool_version_id = version.json()["data"]["id"]
    published = await client.post(
        f"/api/z-torque/skills-tools/tool-versions/{tool_version_id}/publish",
        headers=headers,
    )
    assert published.status_code == 200
    return tool_version_id


async def _create_draft_skill_version(client: AsyncClient, headers: dict[str, str]) -> str:
    skill = await client.post(
        "/api/z-torque/skills-tools/skills",
        headers=headers,
        json={"name": f"skill-{uuid4().hex[:8]}", "description": "s", "skill_type": "STANDARD"},
    )
    assert skill.status_code == 200
    skill_id = skill.json()["data"]["id"]
    version = await client.post(
        f"/api/z-torque/skills-tools/skills/{skill_id}/versions",
        headers=headers,
        json={},
    )
    assert version.status_code == 200
    return version.json()["data"]["id"]


@pytest.mark.asyncio
async def test_skill_publish_applies_unsaved_changes_atomically(tenant_user):
    """B-1：发布请求体携带未保存修改，落库+发布一次完成，确认开关进入已发布版本。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client)
        version_id = await _create_draft_skill_version(client, headers)
        resp = await client.post(
            f"/api/z-torque/skills-tools/skill-versions/{version_id}/publish",
            headers=headers,
            json={"instructions": "hello", "requires_confirmation": True, "required_permissions": ["net"]},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "PUBLISHED"
        detail = await client.get(f"/api/z-torque/skills-tools/skill-versions/{version_id}", headers=headers)
        data = detail.json()["data"]
        assert data["instructions"] == "hello"
        assert data["requires_confirmation"] is True
        assert data["required_permissions"] == ["net"]
        assert data["snapshot_hash"]


@pytest.mark.asyncio
async def test_skill_publish_replaces_bindings(tenant_user):
    """B-1：发布请求体 bindings 整体替换绑定列表并随快照固化。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client)
        tool_a = await _create_published_tool(client, headers)
        tool_b = await _create_published_tool(client, headers)
        version_id = await _create_draft_skill_version(client, headers)
        attach = await client.post(
            f"/api/z-torque/skills-tools/skill-versions/{version_id}/tools",
            headers=headers,
            json={"tool_version_id": tool_a},
        )
        assert attach.status_code == 200
        resp = await client.post(
            f"/api/z-torque/skills-tools/skill-versions/{version_id}/publish",
            headers=headers,
            json={"instructions": "x", "bindings": [{"tool_version_id": tool_b, "note": "主工具"}]},
        )
        assert resp.status_code == 200
        tools = await client.get(f"/api/z-torque/skills-tools/skill-versions/{version_id}/tools", headers=headers)
        items = tools.json()["data"]
        assert [item["tool_version_id"] for item in items] == [tool_b]
        assert items[0]["note"] == "主工具"
        assert items[0]["sort_order"] == 0


@pytest.mark.asyncio
async def test_skill_publish_rejects_unpublished_binding_and_rolls_back(tenant_user):
    """B-1：bindings 引用未发布工具版本时报错且不落任何数据（版本仍为 DRAFT、绑定不变）。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client)
        tool_a = await _create_published_tool(client, headers)
        version_id = await _create_draft_skill_version(client, headers)
        attach = await client.post(
            f"/api/z-torque/skills-tools/skill-versions/{version_id}/tools",
            headers=headers,
            json={"tool_version_id": tool_a},
        )
        assert attach.status_code == 200
        resp = await client.post(
            f"/api/z-torque/skills-tools/skill-versions/{version_id}/publish",
            headers=headers,
            json={"bindings": [{"tool_version_id": str(uuid4())}]},
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "AI_TOOL_VERSION_NOT_FOUND"
        detail = await client.get(f"/api/z-torque/skills-tools/skill-versions/{version_id}", headers=headers)
        data = detail.json()["data"]
        assert data["status"] == "DRAFT"
        assert data["tool_version_ids"] == [tool_a]


@pytest.mark.asyncio
async def test_skill_publish_without_body_still_works(tenant_user):
    """B-1 回归：不带请求体的发布（扫描草稿确认启用等入口）不受影响。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client)
        version_id = await _create_draft_skill_version(client, headers)
        resp = await client.post(
            f"/api/z-torque/skills-tools/skill-versions/{version_id}/publish",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "PUBLISHED"


@pytest.mark.asyncio
async def test_agent_create_publishes_in_one_step(tenant_user):
    """B-2：创建智能体 + 首版本 + 绑定技能 + 发布一步完成。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client)
        # 准备一个已发布的技能版本
        skill_version_id = await _create_draft_skill_version(client, headers)
        pub = await client.post(
            f"/api/z-torque/skills-tools/skill-versions/{skill_version_id}/publish",
            headers=headers,
            json={"instructions": "ready"},
        )
        assert pub.status_code == 200
        resp = await client.post(
            "/api/z-torque/agents",
            headers=headers,
            json={
                "name": "one-shot-agent",
                "description": "d",
                "publish": True,
                "config_snapshot": {"system_prompt": "sp", "model": "gpt-x"},
                "skill_version_ids": [skill_version_id],
            },
        )
        assert resp.status_code == 200
        agent = resp.json()["data"]
        assert agent["current_published_version_id"]
        skills = await client.get(
            f"/api/z-torque/agents/{agent['id']}/versions/{agent['current_published_version_id']}/skills",
            headers=headers,
        )
        items = skills.json()["data"]
        assert [item["skill_version_id"] for item in items] == [skill_version_id]
        assert items[0]["status"] == "PUBLISHED"


@pytest.mark.asyncio
async def test_agent_create_publish_rejects_unpublished_skill_and_rolls_back(tenant_user):
    """B-2：绑定的技能版本未发布时返回明确错误码，且整个创建事务回滚。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client)
        skill_version_id = await _create_draft_skill_version(client, headers)  # 保持 DRAFT
        resp = await client.post(
            "/api/z-torque/agents",
            headers=headers,
            json={
                "name": "rollback-agent",
                "publish": True,
                "config_snapshot": {"system_prompt": "sp", "model": "gpt-x"},
                "skill_version_ids": [skill_version_id],
            },
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "AI_AGENT_SKILL_VERSION_NOT_PUBLISHED"
        listing = await client.get("/api/z-torque/agents", headers=headers)
        names = [item["name"] for item in listing.json()["data"]]
        assert "rollback-agent" not in names


@pytest.mark.asyncio
async def test_agent_create_publish_requires_snapshot(tenant_user):
    """B-2：publish=true 但快照为空时拒绝发布（沿用版本配置非空校验）。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client)
        resp = await client.post(
            "/api/z-torque/agents",
            headers=headers,
            json={"name": "empty-snapshot-agent", "publish": True, "config_snapshot": {}},
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "AI_REQUEST_INVALID"
