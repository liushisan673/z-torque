"""Phase 2 阶段二：repositories 属主过滤收口真实入口验收。

负向先行：同租户内他人私有资源不可读、不可作为工具/目标/会话上下文使用；
OFFICIAL 资源仍对同租户全体用户可见（与既有 agent 解析口径一致）。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.api.routes.webhooks import _require_agent
from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.models import (
    Agent,
    AgentVersion,
    AgentVersionSkill,
    Conversation,
    Message,
    Skill,
    SkillVersion,
    SkillVersionTool,
    Tenant,
    Tool,
    ToolVersion,
    User,
)
from app.repositories import get_owned, get_owned_version
from app.services.stream_service import _owned_stream_resources
from app.services.tool_mapping_builder import build_tool_mappings

_CREATED_TENANTS: list = []


@pytest.fixture(autouse=True)
def cleanup():
    _CREATED_TENANTS.clear()
    yield
    with SessionLocal.begin() as db:
        if _CREATED_TENANTS:
            for model in (
                SkillVersionTool, AgentVersionSkill, ToolVersion, Tool, SkillVersion, Skill,
                Message, Conversation, AgentVersion, Agent, User,
            ):
                db.execute(delete(model).where(model.tenant_id.in_(_CREATED_TENANTS)))
            db.execute(delete(Tenant).where(Tenant.id.in_(_CREATED_TENANTS)))
    _CREATED_TENANTS.clear()


def _bootstrap_two_users():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"p2own-{uuid4()}")
        db.add(tenant)
        db.flush()
        a = User(tenant_id=tenant.id, username=f"a-{uuid4()}", password_hash="x")
        b = User(tenant_id=tenant.id, username=f"b-{uuid4()}", password_hash="x")
        db.add_all([a, b])
        db.flush()
        _CREATED_TENANTS.append(tenant.id)
        return tenant.id, a.id, b.id


def _make_agent(db, tenant_id, owner_user_id, *, source_type="USER", name="agent"):
    agent = Agent(
        tenant_id=tenant_id, owner_user_id=owner_user_id, name=name, description="",
        source_type=source_type, status="ACTIVE",
    )
    db.add(agent)
    db.flush()
    version = AgentVersion(
        tenant_id=tenant_id, agent_id=agent.id, version_number=1, status="PUBLISHED",
        config_schema_version="1", config_snapshot={},
    )
    db.add(version)
    db.flush()
    agent.current_published_version_id = version.id
    db.flush()
    return agent, version


def _make_published_tool(db, tenant_id, owner_user_id, name):
    tool = Tool(
        tenant_id=tenant_id, owner_user_id=owner_user_id, name=name, description="",
        tool_type="HTTP", source_type="USER", status="ACTIVE", official=False,
        tags=[], is_business_tool=False,
    )
    db.add(tool)
    db.flush()
    version = ToolVersion(
        tenant_id=tenant_id, tool_id=tool.id, version_number=1, status="PUBLISHED",
        config_schema_version="1", config_snapshot={},
        input_schema={"type": "object", "properties": {}}, required_permissions=[],
        requires_confirmation=False, timeout_seconds=30.0, max_retries=0, retry_backoff_seconds=0.5,
    )
    db.add(version)
    db.flush()
    tool.current_published_version_id = version.id
    db.flush()
    return tool, version


def _make_published_skill(db, tenant_id, owner_user_id, name, tool_versions):
    skill = Skill(
        tenant_id=tenant_id, owner_user_id=owner_user_id, name=name, description="",
        skill_type="STANDARD", source_type="USER", status="ACTIVE", official=False,
    )
    db.add(skill)
    db.flush()
    version = SkillVersion(
        tenant_id=tenant_id, skill_id=skill.id, version_number=1, status="PUBLISHED",
        config_schema_version="1", config_snapshot={}, required_permissions=[],
        requires_confirmation=False,
    )
    db.add(version)
    db.flush()
    skill.current_published_version_id = version.id
    for index, tool_version in enumerate(tool_versions):
        db.add(SkillVersionTool(
            tenant_id=tenant_id, skill_version_id=version.id,
            tool_version_id=tool_version.id, sort_order=index,
        ))
    db.flush()
    return skill, version


def test_get_owned_rejects_foreign_private_and_allows_official():
    """负向/正向：他人私有资源 404；OFFICIAL 资源同租户可见。"""
    tenant_id, a_id, b_id = _bootstrap_two_users()
    with SessionLocal.begin() as db:
        private, _ = _make_agent(db, tenant_id, a_id, name="A-private")
        official, _ = _make_agent(db, tenant_id, None, source_type="OFFICIAL", name="platform")
        private_id, official_id = private.id, official.id
    with SessionLocal() as db:
        assert get_owned(db, Agent, resource_id=private_id, tenant_id=tenant_id, user_id=a_id).id == private_id
        with pytest.raises(ApiError) as exc:
            get_owned(db, Agent, resource_id=private_id, tenant_id=tenant_id, user_id=b_id)
        assert exc.value.code == "AI_RESOURCE_NOT_FOUND" and exc.value.status_code == 404
        assert get_owned(db, Agent, resource_id=official_id, tenant_id=tenant_id, user_id=b_id).id == official_id


def test_get_owned_version_checks_parent_owner():
    """负向：版本表通过父资源属主判定，他人私有 agent 的版本不可见。"""
    tenant_id, a_id, b_id = _bootstrap_two_users()
    with SessionLocal.begin() as db:
        _, version = _make_agent(db, tenant_id, a_id, name="A-private")
        version_id = version.id
    with SessionLocal() as db:
        with pytest.raises(ApiError) as exc:
            get_owned_version(
                db, AgentVersion, Agent, "agent_id", version_id=version_id,
                tenant_id=tenant_id, user_id=b_id,
            )
        assert exc.value.status_code == 404
        assert get_owned_version(
            db, AgentVersion, Agent, "agent_id", version_id=version_id,
            tenant_id=tenant_id, user_id=a_id,
        ).id == version_id


def test_build_tool_mappings_filters_foreign_private_tools():
    """真实入口：工具目录按当前用户属主过滤，他人私有 Skill/Tool 不暴露给模型。"""
    tenant_id, a_id, b_id = _bootstrap_two_users()
    with SessionLocal.begin() as db:
        _, agent_version = _make_agent(db, tenant_id, a_id, name="agent")
        _, tool_version = _make_published_tool(db, tenant_id, a_id, "private_tool")
        _, skill_version = _make_published_skill(db, tenant_id, a_id, "private_skill", [tool_version])
        db.add(AgentVersionSkill(
            tenant_id=tenant_id, agent_version_id=agent_version.id,
            skill_version_id=skill_version.id,
        ))
        db.flush()
        version_id = agent_version.id
    with SessionLocal() as db:
        version = db.get(AgentVersion, version_id)
        own_mappings, _ = build_tool_mappings(db, version, tenant_id, user_id=a_id)
        foreign_mappings, _ = build_tool_mappings(db, version, tenant_id, user_id=b_id)
        assert "private_tool" in own_mappings, "属主应看到自己发布的私有工具"
        assert "private_tool" not in foreign_mappings, "非属主不得看到他人私有工具"
        # 平台内置工具（如文档生成）不属于任何用户的私有资源，应继续对用户可见。


def test_stream_resources_reject_foreign_conversation():
    """真实入口：流式写入前的会话/消息属主校验，越权会话直接 404。"""
    tenant_id, a_id, b_id = _bootstrap_two_users()
    with SessionLocal.begin() as db:
        agent, _ = _make_agent(db, tenant_id, a_id, name="stream-agent")
        conversation = Conversation(tenant_id=tenant_id, owner_user_id=a_id, agent_id=agent.id, title="p2")
        db.add(conversation)
        db.flush()
        message = Message(
            tenant_id=tenant_id, conversation_id=conversation.id,
            sender_type="USER", content="hi",
        )
        db.add(message)
        db.flush()
        conversation_id, message_id = conversation.id, message.id
    with SessionLocal() as db:
        with pytest.raises(ApiError) as exc:
            _owned_stream_resources(
                db, tenant_id=tenant_id, user_id=b_id, conversation_id=conversation_id,
                user_message_id=message_id, agent_version_id=None,
            )
        assert exc.value.status_code == 404
        conversation, message, _ = _owned_stream_resources(
            db, tenant_id=tenant_id, user_id=a_id, conversation_id=conversation_id,
            user_message_id=message_id, agent_version_id=None,
        )
        assert conversation.id == conversation_id and message.id == message_id


def test_webhook_target_agent_must_be_owner_visible():
    """API 守卫：webhook 目标智能体不得指向同租户他人的私有智能体。"""
    tenant_id, a_id, b_id = _bootstrap_two_users()
    with SessionLocal.begin() as db:
        private, _ = _make_agent(db, tenant_id, a_id, name="A-private")
        agent_id = private.id
    with SessionLocal() as db:
        with pytest.raises(ApiError) as exc:
            _require_agent(db, db.get(User, b_id), agent_id)
        assert exc.value.code == "AI_AGENT_NOT_FOUND"
        assert _require_agent(db, db.get(User, a_id), agent_id).id == agent_id
