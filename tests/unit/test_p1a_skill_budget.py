from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import delete

from app.db.session import SessionLocal
from app.models import Agent, AgentVersion, AgentVersionSkill, Skill, SkillVersion, Tenant, User
from app.services.context_builder import build_agent_system_prompt
from tests.support.db_cleanup import cleanup_session


def test_three_tier_skill_injection_respects_hard_budget(monkeypatch):
    monkeypatch.setattr("app.services.context_builder.get_settings", lambda: SimpleNamespace(skill_inject_max_chars=350, skill_inject_max_items=5))
    with cleanup_session() as db:
        for model in (AgentVersionSkill, SkillVersion, Skill, AgentVersion, Agent, User, Tenant):
            db.execute(delete(model))
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"skill-budget-{uuid4()}")
        db.add(tenant); db.flush()
        owner = User(tenant_id=tenant.id, username=f"skill-budget-{uuid4()}", password_hash="x")
        db.add(owner); db.flush()
        agent = Agent(tenant_id=tenant.id, owner_user_id=owner.id, name="A", source_type="USER", status="ACTIVE")
        db.add(agent); db.flush()
        av = AgentVersion(tenant_id=tenant.id, agent_id=agent.id, version_number=1, status="PUBLISHED", config_schema_version="1", config_snapshot={"skill_inject_scopes": ["WORKSPACE", "USER", "AGENT"]})
        db.add(av); db.flush()
        versions = []
        for label, official, source in (("workspace", True, "SYSTEM"), ("user", False, "USER"), ("agent", False, "USER")):
            skill = Skill(tenant_id=tenant.id, owner_user_id=owner.id if source == "USER" else None, name=label, description=f"{label} desc", skill_type="STANDARD", source_type=source, status="ACTIVE", official=official)
            db.add(skill); db.flush()
            version = SkillVersion(tenant_id=tenant.id, skill_id=skill.id, version_number=1, status="PUBLISHED", config_schema_version="1", config_snapshot={}, instructions=f"{label} " * 12, requires_confirmation=False)
            db.add(version); db.flush()
            skill.current_published_version_id = version.id
            versions.append(version)
        db.add(AgentVersionSkill(tenant_id=tenant.id, agent_version_id=av.id, skill_version_id=versions[2].id))
        db.flush()
        prompt = build_agent_system_prompt(db, av)
        assert "技能背景/WORKSPACE" in prompt
        assert "技能背景/USER" in prompt
        assert "技能背景/AGENT" in prompt
        assert prompt.index("技能背景/WORKSPACE") < prompt.index("技能背景/USER") < prompt.index("技能背景/AGENT")
        assert len(prompt) < 600
