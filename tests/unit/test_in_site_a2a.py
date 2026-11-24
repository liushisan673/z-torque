"""站内 A2A：系统自带智能体（is_default_parent）自动派发工具的映射测试。"""

from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.kernel.agent.service import ensure_system_agent
from app.models import Agent, AgentVersion, Tenant
from app.services.tool_mapping_builder import build_tool_mappings
from tests.support.ddl import fk_off, fk_on


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with SessionLocal.begin() as db:
        fk_off(db)
        db.execute(delete(AgentVersion))
        db.execute(delete(Agent))
        db.execute(delete(Tenant))
        fk_on(db)


def _agents(db, *, system_parent: bool, user_default: bool = False):
    tenant = Tenant(name=f"a2a-{uuid4()}")
    db.add(tenant)
    db.flush()
    target = Agent(tenant_id=tenant.id, owner_user_id=None, name="写作智能体", description="写报告", source_type="OFFICIAL", status="ACTIVE")
    db.add(target)
    db.flush()
    target_version = AgentVersion(tenant_id=tenant.id, agent_id=target.id, version_number=1, status="PUBLISHED", config_schema_version="1", config_snapshot={})
    db.add(target_version)
    db.flush()
    system = Agent(tenant_id=tenant.id, owner_user_id=None, name="系统智能体", description="路由需求", source_type="OFFICIAL", status="ACTIVE", is_default=user_default, is_default_parent=system_parent)
    db.add(system)
    db.flush()
    system_version = AgentVersion(tenant_id=tenant.id, agent_id=system.id, version_number=1, status="PUBLISHED", config_schema_version="1", config_snapshot={})
    db.add(system_version)
    db.flush()
    return tenant.id, system_version, target_version


def test_builtin_system_agent_exposes_dispatch_tool():
    """系统自带智能体（默认父）暴露派发工具，普通智能体不暴露。"""
    with SessionLocal.begin() as db:
        tenant_id, system_version, target_version = _agents(db, system_parent=True)
        mappings, tools = build_tool_mappings(db, system_version, tenant_id)
        dispatch = [mapping for mapping in mappings.values() if mapping.dispatch_agent_version_id is not None]
        assert len(dispatch) == 1
        assert dispatch[0].dispatch_agent_version_id == target_version.id
        assert any(tool["function"]["name"] == dispatch[0].model_name for tool in tools)
        assert dispatch[0].input_schema["required"] == ["task"]

    with SessionLocal.begin() as db:
        tenant_id, system_version, _ = _agents(db, system_parent=False)
        mappings, _ = build_tool_mappings(db, system_version, tenant_id)
        assert [m for m in mappings.values() if m.dispatch_agent_version_id is not None] == []


def test_user_default_agent_does_not_get_dispatch_tool():
    """用户个人默认智能体不获得跨智能体调度能力，只有系统自带智能体可以。"""
    with SessionLocal.begin() as db:
        tenant_id, system_version, _ = _agents(db, system_parent=False, user_default=True)
        mappings, _ = build_tool_mappings(db, system_version, tenant_id)
        assert [m for m in mappings.values() if m.dispatch_agent_version_id is not None] == []


def test_ensure_system_agent_is_idempotent_and_published():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"sys-{uuid4()}")
        db.add(tenant)
        db.flush()
        first = ensure_system_agent(db, tenant_id=tenant.id)
        first_id = first.id
        second = ensure_system_agent(db, tenant_id=tenant.id)
        assert second.id == first_id
        assert second.is_default_parent is True
        assert second.current_published_version_id is not None


def test_ensure_system_agent_migrates_legacy_mock_snapshot():
    """旧安装的默认父是 mode=mock 空壳：ensure 必须就地升级为 router 快照。"""
    from app.models import AgentVersion

    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"legacy-{uuid4()}")
        db.add(tenant)
        db.flush()
        legacy = Agent(tenant_id=tenant.id, owner_user_id=None, name="Z-Torque Default Parent",
                       source_type="OFFICIAL", status="ACTIVE", is_default_parent=True)
        db.add(legacy)
        db.flush()
        version = AgentVersion(tenant_id=tenant.id, agent_id=legacy.id, version_number=1,
                               status="PUBLISHED", config_schema_version="1", config_snapshot={"mode": "mock"})
        db.add(version)
        db.flush()
        legacy.current_published_version_id = version.id

        upgraded = ensure_system_agent(db, tenant_id=tenant.id)
        assert upgraded.id == legacy.id
        assert "系统智能体" in upgraded.name
        assert db.get(AgentVersion, version.id).config_snapshot["mode"] == "router"
