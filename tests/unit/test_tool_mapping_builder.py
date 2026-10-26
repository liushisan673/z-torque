"""工具映射 schema 净化测试（悬空 $ref 防护，修复 OpenAPI 导入工具导致上游 400）。"""

from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.models import (
    Agent,
    AgentVersion,
    AgentVersionSkill,
    Skill,
    SkillVersion,
    SkillVersionTool,
    Tenant,
    Tool,
    ToolVersion,
)
from app.services.tool_mapping_builder import _sanitize_schema, build_tool_mappings
from tests.support.ddl import fk_off, fk_on


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with SessionLocal.begin() as db:
        # 清理会跨用例触碰到别人留下的引用行，先关外键再按依赖顺序删
        fk_off(db)
        db.execute(delete(AgentVersionSkill))
        db.execute(delete(SkillVersionTool))
        db.execute(delete(ToolVersion))
        db.execute(delete(Tool))
        db.execute(delete(SkillVersion))
        db.execute(delete(Skill))
        db.execute(delete(AgentVersion))
        db.execute(delete(Agent))
        db.execute(delete(Tenant))
        fk_on(db)


def _fixture(db, input_schema, *, skill_requires_confirmation: bool = False, tool_requires_confirmation: bool = False):
    tenant = Tenant(name=f"tmb-{uuid4()}")
    db.add(tenant)
    db.flush()
    agent = Agent(tenant_id=tenant.id, owner_user_id=None, name="A", source_type="USER", status="ACTIVE")
    db.add(agent)
    db.flush()
    version = AgentVersion(tenant_id=tenant.id, agent_id=agent.id, version_number=1, status="PUBLISHED", config_schema_version="1", config_snapshot={})
    db.add(version)
    db.flush()
    skill = Skill(tenant_id=tenant.id, owner_user_id=None, name="S", description=None, skill_type="STANDARD", source_type="USER", status="ACTIVE", official=False)
    db.add(skill)
    db.flush()
    skill_version = SkillVersion(tenant_id=tenant.id, skill_id=skill.id, version_number=1, status="PUBLISHED", config_schema_version="1", config_snapshot={}, requires_confirmation=skill_requires_confirmation)
    db.add(skill_version)
    db.flush()
    tool = Tool(tenant_id=tenant.id, owner_user_id=None, name="cordys_list", description="query customers", tool_type="HTTP", source_type="USER", status="ACTIVE", official=False)
    db.add(tool)
    db.flush()
    tool_version = ToolVersion(tenant_id=tenant.id, tool_id=tool.id, version_number=1, status="PUBLISHED", config_schema_version="1", config_snapshot={}, input_schema=input_schema, requires_confirmation=tool_requires_confirmation)
    db.add(tool_version)
    db.flush()
    db.add(AgentVersionSkill(tenant_id=tenant.id, agent_version_id=version.id, skill_version_id=skill_version.id))
    db.add(SkillVersionTool(tenant_id=tenant.id, skill_version_id=skill_version.id, tool_version_id=tool_version.id))
    db.flush()
    return tenant, version


def test_dangling_ref_is_stripped_from_model_tools_and_mapping():
    """OpenAPI 导入残留的 #/components 悬空引用必须剥离（上游 400 根因）。"""
    broken = {
        "type": "object",
        "properties": {
            "CSRF-TOKEN": {"type": "string"},
            "__body__": {"$ref": "#/components/schemas/CustomerPageRequest", "description": "request body (JSON object)"},
        },
        "required": ["CSRF-TOKEN"],
    }
    with SessionLocal() as db:
        tenant, version = _fixture(db, broken)
        mappings, tools = build_tool_mappings(db, version, tenant.id)
        assert len(tools) == 1
        parameters = tools[0]["function"]["parameters"]
        dumped = repr(parameters)
        assert "$ref" not in dumped and "components" not in dumped
        body_prop = parameters["properties"]["__body__"]
        assert body_prop == {"type": "object", "description": "request body (JSON object)"}
        # 执行侧映射使用同一净化后 schema（两侧一致）
        assert mappings["cordys_list"].input_schema is parameters
        db.rollback()


def test_nested_ref_inside_items_is_stripped():
    schema = {"type": "object", "properties": {"rows": {"type": "array", "items": {"$ref": "#/components/schemas/Row"}}}}
    sanitized = _sanitize_schema(schema)
    assert sanitized["properties"]["rows"]["items"] == {"type": "object"}


def test_clean_schema_passes_through_unchanged():
    schema = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    assert _sanitize_schema(schema) == schema


def test_ref_without_description_replaced_by_plain_object():
    assert _sanitize_schema({"$ref": "#/components/schemas/X"}) == {"type": "object"}


def test_delegation_mapping_excludes_confirmation_tools():
    """委派子代理不可见需人工确认的工具：子执行链无 checkpoint 挂起语义，
    若不排除，写入类工具会绕过人工审批直接执行。"""
    with SessionLocal() as db:
        tenant, version = _fixture(db, {"type": "object", "properties": {}}, skill_requires_confirmation=True)
        mappings, tools = build_tool_mappings(db, version, tenant.id, exclude_requires_confirmation=True)
        assert mappings == {} and tools == []
        # 工具级确认标记同样被排除
        tenant2, version2 = _fixture(db, {"type": "object", "properties": {}}, tool_requires_confirmation=True)
        mappings2, tools2 = build_tool_mappings(db, version2, tenant2.id, exclude_requires_confirmation=True)
        assert mappings2 == {} and tools2 == []
        # 主链路（父智能体）默认不受影响：确认工具仍然暴露，走人工审批
        mappings3, tools3 = build_tool_mappings(db, version, tenant.id)
        assert "cordys_list" in mappings3 and len(tools3) == 1
