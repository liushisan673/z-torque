"""P1a-4：工具选择器 + depends_on + orchestration_hint 真实入口验收。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.kernel.skill_service import publish_skill_version
from app.main import app
from app.models import (
    Agent,
    AgentVersion,
    AgentVersionSkill,
    Permission,
    Role,
    Skill,
    SkillVersion,
    Tenant,
    Tool,
    ToolVersion,
    User,
    role_permission,
    user_role,
)
from app.security.passwords import hash_password
from app.services.context_builder import build_agent_system_prompt
from app.services.tool_mapping_builder import build_tool_mappings
from tests.support.ddl import fk_off, fk_on

SKILLS = "/api/z-torque/skills-tools"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with SessionLocal.begin() as db:
        fk_off(db)
        for model in (AgentVersionSkill, SkillVersion, ToolVersion, Tool, Skill, AgentVersion, Agent, user_role, role_permission, Permission, Role, User, Tenant):
            db.execute(delete(model))
        fk_on(db)


def _bootstrap():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"tool-sel-{uuid4()}")
        db.add(tenant); db.flush()
        role = Role(tenant_id=tenant.id, name="admin"); db.add(role); db.flush()
        perms = [Permission(tenant_id=tenant.id, name=name) for name in ("skill.manage", "skill.read", "tool.read")]
        db.add_all(perms); db.flush()
        for perm in perms:
            db.execute(role_permission.insert().values(role_id=role.id, permission_id=perm.id))
        user = User(tenant_id=tenant.id, username=f"sel-{uuid4()}", password_hash=hash_password("pass")); db.add(user); db.flush()
        db.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
        skill = Skill(tenant_id=tenant.id, owner_user_id=user.id, name="S", description="d", skill_type="STANDARD", source_type="USER", status="ACTIVE", official=False); db.add(skill); db.flush()
        sv = SkillVersion(tenant_id=tenant.id, skill_id=skill.id, version_number=1, status="DRAFT", config_schema_version="1", config_snapshot={}, instructions="INSTRUCTIONS", orchestration_hint="先依赖后主工具", requires_confirmation=False); db.add(sv); db.flush()
        tools = []
        for name in ("tool_a", "tool_b"):
            tool = Tool(tenant_id=tenant.id, owner_user_id=user.id, name=name, description="d", tool_type="HTTP", source_type="USER", status="ACTIVE", official=False)
            db.add(tool); db.flush()
            version = ToolVersion(tenant_id=tenant.id, tool_id=tool.id, version_number=1, status="PUBLISHED", config_snapshot={}, input_schema={"type": "object", "properties": {}}, required_permissions=[], requires_confirmation=False, timeout_seconds=5, snapshot_hash="x")
            db.add(version); db.flush()
            tools.append(version.id)
        agent = Agent(tenant_id=tenant.id, owner_user_id=user.id, name="A", source_type="USER", status="ACTIVE"); db.add(agent); db.flush()
        av = AgentVersion(tenant_id=tenant.id, agent_id=agent.id, version_number=1, status="PUBLISHED", config_schema_version="1", config_snapshot={}); db.add(av); db.flush()
        db.add(AgentVersionSkill(tenant_id=tenant.id, agent_version_id=av.id, skill_version_id=sv.id)); db.flush()
        return tenant.id, user.id, user.username, sv.id, tools, av


async def _login(client: AsyncClient, username: str):
    response = await client.post("/api/z-torque/auth/login", json={"username": username, "password": "pass"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


@pytest.mark.asyncio
async def test_selector_depends_on_and_hint_are_visible_to_model():
    tenant_id, user_id, username, skill_version_id, (tool_a, tool_b), agent_version = _bootstrap()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, username)
        first = await client.post(f"{SKILLS}/skill-versions/{skill_version_id}/tools", headers=headers, json={"tool_version_id": str(tool_b), "sort_order": 1})
        assert first.status_code == 200, first.text
        second = await client.post(f"{SKILLS}/skill-versions/{skill_version_id}/tools", headers=headers, json={"tool_version_id": str(tool_a), "sort_order": 0, "selector": {"tool_types": ["MCP"], "tags": ["retrieval"]}, "depends_on": [str(tool_b)]})
        assert second.status_code == 200, second.text
        assert second.json()["data"]["selector"]["tool_types"] == ["MCP"]
        assert second.json()["data"]["depends_on"] == [str(tool_b)]

    with SessionLocal.begin() as db:
        publish_skill_version(db, tenant_id=tenant_id, user_id=user_id, version_id=skill_version_id)
        _mappings, tools = build_tool_mappings(db, agent_version, tenant_id)
        prompt = build_agent_system_prompt(db, agent_version)
        assert [item["function"]["name"] for item in tools] == ["tool_a", "tool_b"]
        assert prompt.index("1. tool_b") < prompt.index("2. tool_a")
        assert "依赖：tool_b" in prompt
        assert "选择条件：tool_types=MCP" in prompt
        assert "先依赖后主工具" in prompt
