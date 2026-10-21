"""工具绑定治理：标签规范化、绑定角色校验、业务封装参数模板解析、绑定升级。"""

from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.kernel.skill_gateway import (
    _resolve_composite_arguments,
    _resolve_composite_value,
    _resolve_step_arguments,
    _select_composite_output,
)
from app.kernel.skill_service import (
    MAX_TOOL_TAG_LENGTH,
    MAX_TOOL_TAGS,
    normalize_tags,
    upgrade_tool_binding,
    validate_binding_role,
)
from app.models import Skill, SkillVersion, SkillVersionTool, Tenant, Tool, ToolVersion, User


def test_normalize_tags_trims_dedupes_and_limits():
    tags = [" 客户 ", "", "客户", "查询", "x" * (MAX_TOOL_TAG_LENGTH + 8)]
    result = normalize_tags(tags)
    assert result[:2] == ["客户", "查询"]
    assert len(result[2]) == MAX_TOOL_TAG_LENGTH


def test_normalize_tags_caps_count():
    result = normalize_tags([f"tag{i}" for i in range(MAX_TOOL_TAGS + 5)])
    assert len(result) == MAX_TOOL_TAGS


def test_normalize_tags_handles_none():
    assert normalize_tags(None) == []


def test_validate_binding_role_accepts_known_roles():
    assert validate_binding_role("prerequisite") == "prerequisite"
    assert validate_binding_role(" normal ") == "normal"
    assert validate_binding_role("followup") == "followup"
    assert validate_binding_role(None) is None
    assert validate_binding_role("") is None


def test_validate_binding_role_rejects_unknown():
    with pytest.raises(ApiError) as exc:
        validate_binding_role("parallel")
    assert exc.value.code == "AI_REQUEST_INVALID"


def test_resolve_composite_arguments_supports_input_and_step_templates():
    arguments = {
        "customer_id": "$input.customer_id",
        "name": "$steps.step1.output.data.name",
        "rows": ["$steps.step1.output.items[0].id"],
        "literal": "fixed",
    }
    resolved = _resolve_composite_arguments(
        arguments,
        {"customer_id": "C-1"},
        {"step1": {"data": {"name": "张三"}, "items": [{"id": 7}]}},
    )
    assert resolved == {"customer_id": "C-1", "name": "张三", "rows": [7], "literal": "fixed"}


def test_resolve_composite_arguments_missing_path_raises():
    with pytest.raises(KeyError):
        _resolve_composite_arguments({"x": "$steps.step9.output.id"}, {}, {})


def test_resolve_composite_value_keeps_scalars():
    assert _resolve_composite_value(3, {}, {}) == 3
    assert _resolve_composite_value(["a", 1], {}, {}) == ["a", 1]


def test_select_composite_output_reads_nested_path():
    assert _select_composite_output("$steps.step2.output.result.total", {"step2": {"result": {"total": 12}}}) == 12


def test_select_composite_output_rejects_non_step_expression():
    with pytest.raises(ValueError):
        _select_composite_output("$.result", {})


def test_resolve_step_arguments_blank_passes_through_filtered_by_child_schema():
    """留空步骤参数 = 透传调用入参，并按子工具声明属性过滤（非专业用户兜底）。"""
    inputs = {"location": "宝鸡", "extra": "ignored"}
    child_schema = {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]}
    assert _resolve_step_arguments({}, inputs, {}, child_schema) == {"location": "宝鸡"}
    # 子工具未声明属性：原样透传
    assert _resolve_step_arguments(None, inputs, {}, {}) == inputs


def test_resolve_step_arguments_explicit_templates_still_resolved():
    resolved = _resolve_step_arguments(
        {"location": "$input.location", "name": "$steps.step1.output.name"},
        {"location": "西安"},
        {"step1": {"name": "钟楼"}},
        {},
    )
    assert resolved == {"location": "西安", "name": "钟楼"}


# ---------- 绑定升级（同一工具新发布版本） ----------

@pytest.fixture()
def binding_cleanup():
    yield
    with SessionLocal.begin() as db:
        db.execute(delete(SkillVersionTool))
        db.execute(delete(ToolVersion))
        db.execute(delete(Tool))
        db.execute(delete(SkillVersion))
        db.execute(delete(Skill))
        db.execute(delete(User))
        db.execute(delete(Tenant))


def _binding_fixture(db, *, skill_status: str = "DRAFT"):
    tenant = Tenant(name=f"tupgrade-{uuid4()}")
    db.add(tenant)
    db.flush()
    owner = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash="x")
    db.add(owner)
    db.flush()
    skill = Skill(tenant_id=tenant.id, owner_user_id=owner.id, name="S", skill_type="STANDARD", source_type="USER", status="ACTIVE", official=False)
    db.add(skill)
    db.flush()
    sv = SkillVersion(tenant_id=tenant.id, skill_id=skill.id, version_number=1, status=skill_status, config_schema_version="1", config_snapshot={})
    db.add(sv)
    db.flush()
    tool = Tool(tenant_id=tenant.id, owner_user_id=owner.id, name="http_tool", tool_type="HTTP", source_type="CONNECTOR", status="ACTIVE", official=False)
    db.add(tool)
    db.flush()
    v1 = ToolVersion(tenant_id=tenant.id, tool_id=tool.id, version_number=1, status="PUBLISHED", config_schema_version="1", config_snapshot={})
    v2 = ToolVersion(tenant_id=tenant.id, tool_id=tool.id, version_number=2, status="PUBLISHED", config_schema_version="1", config_snapshot={})
    other = ToolVersion(tenant_id=tenant.id, tool_id=tool.id, version_number=3, status="DRAFT", config_schema_version="1", config_snapshot={})
    db.add_all([v1, v2, other])
    db.flush()
    link = SkillVersionTool(tenant_id=tenant.id, skill_version_id=sv.id, tool_version_id=v1.id, sort_order=3, role="normal", note="keep")
    db.add(link)
    db.flush()
    return tenant, owner, sv, v1, v2, other, link


def test_upgrade_tool_binding_preserves_order_role_note(binding_cleanup):
    with SessionLocal() as db:
        tenant, owner, sv, v1, v2, _other, _link = _binding_fixture(db)
        upgraded = upgrade_tool_binding(
            db, tenant_id=tenant.id, user_id=owner.id, skill_version_id=sv.id, tool_version_id=v1.id, new_tool_version_id=v2.id,
        )
        assert str(upgraded.tool_version_id) == str(v2.id)
        assert upgraded.sort_order == 3
        assert upgraded.role == "normal"
        assert upgraded.note == "keep"
        db.rollback()


def test_upgrade_tool_binding_rejects_cross_tool_and_unpublished(binding_cleanup):
    with SessionLocal() as db:
        tenant, owner, sv, v1, _v2, draft_v3, _link = _binding_fixture(db)
        with pytest.raises(ApiError):
            upgrade_tool_binding(db, tenant_id=tenant.id, user_id=owner.id, skill_version_id=sv.id, tool_version_id=v1.id, new_tool_version_id=draft_v3.id)
        db.rollback()


def test_upgrade_tool_binding_requires_draft_skill_version(binding_cleanup):
    with SessionLocal() as db:
        tenant, owner, sv, v1, v2, _other, _link = _binding_fixture(db, skill_status="PUBLISHED")
        with pytest.raises(ApiError) as exc:
            upgrade_tool_binding(db, tenant_id=tenant.id, user_id=owner.id, skill_version_id=sv.id, tool_version_id=v1.id, new_tool_version_id=v2.id)
        assert exc.value.code == "AI_REQUEST_INVALID"
        db.rollback()
