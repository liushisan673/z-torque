"""Phase 0 §2.2：长期记忆落库真实入口验收。

覆盖：
1. 写入 → 新会话（模拟重启）召回，不再丢失；
2. 三级作用域隔离（跨 agent 不可读 / CONVERSATION 只对所属会话可见）；
3. immutable 键 append-only + 版本链；乐观锁冲突 AI_MEMORY_CONFLICT；
4. soft-delete 后不可召回且 audit 有记录；
5. context_builder 三级分流 + capabilities 上报持久化。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.builtin_plugins.long_memory import DbLongTermMemoryStore
from app.config import get_settings
from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AgentVersion,
    AuditLog,
    Conversation,
    LongMemory,
    LongMemoryVersion,
    Tenant,
    User,
)

_CREATED_TENANTS: list = []


@pytest.fixture(autouse=True)
def _cleanup():
    _CREATED_TENANTS.clear()
    yield
    with SessionLocal.begin() as db:
        if _CREATED_TENANTS:
            for model in (AuditLog, LongMemoryVersion, LongMemory, Conversation, AgentVersion, Agent, User):
                db.execute(delete(model).where(model.tenant_id.in_(_CREATED_TENANTS)))
            db.execute(delete(Tenant).where(Tenant.id.in_(_CREATED_TENANTS)))
    _CREATED_TENANTS.clear()


def _bootstrap():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"longmem-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash="x")
        db.add(user)
        db.flush()
        agents = []
        for index in range(2):
            agent = Agent(tenant_id=tenant.id, owner_user_id=None, name=f"agent-{index}", source_type="OFFICIAL", status="ACTIVE")
            db.add(agent)
            db.flush()
            agents.append(agent.id)
        conversation = Conversation(tenant_id=tenant.id, owner_user_id=user.id, agent_id=agents[0], title="mem")
        db.add(conversation)
        db.flush()
        _CREATED_TENANTS.append(tenant.id)
        return tenant.id, user.id, agents, conversation.id


def test_persist_recall_and_scope_isolation():
    """写入 → 新实例（模拟重启）召回；跨 agent 无授权不可读；CONVERSATION 限定会话。"""
    tenant_id, user_id, (agent_a, agent_b), conversation_id = _bootstrap()
    store = DbLongTermMemoryStore()

    with SessionLocal.begin() as db:
        store.remember_scoped(db, tenant_id=tenant_id, user_id=user_id, agent_id=agent_a, scope="AGENT", key="agent:rule", value="always reply in Chinese", source_ref="test:bootstrap")
        store.remember_scoped(db, tenant_id=tenant_id, user_id=user_id, scope="USER", key="profile:tone", value="concise", source_ref="test:bootstrap")
        store.remember_scoped(db, tenant_id=tenant_id, user_id=user_id, agent_id=agent_a, scope="CONVERSATION", key=f"conversation:{conversation_id}:summary", value="conv-summary", source_ref="test:bootstrap")

    # 模拟进程重启：全新的 store 实例 + 全新的 DB 会话
    fresh = DbLongTermMemoryStore()
    with SessionLocal() as db:
        assert fresh.recall_scoped(db, tenant_id=tenant_id, user_id=user_id, agent_id=agent_a, scope="AGENT", key="agent:rule") == "always reply in Chinese"
        assert fresh.recall_scoped(db, tenant_id=tenant_id, user_id=user_id, scope="USER", key="profile:tone") == "concise"
        assert fresh.recall_scoped(db, tenant_id=tenant_id, user_id=user_id, agent_id=agent_a, scope="CONVERSATION", key=f"conversation:{conversation_id}:summary", conversation_id=conversation_id) == "conv-summary"
        # 跨 agent 读 AGENT 级记忆 → 不可见
        assert fresh.recall_scoped(db, tenant_id=tenant_id, user_id=user_id, agent_id=agent_b, scope="AGENT", key="agent:rule") is None
        # CONVERSATION 错会话 → 不可见
        assert fresh.recall_scoped(db, tenant_id=tenant_id, user_id=user_id, agent_id=agent_a, scope="CONVERSATION", key=f"conversation:{conversation_id}:summary", conversation_id=uuid4()) is None
        bundle = fresh.recall_bundle(db, tenant_id=tenant_id, user_id=user_id, agent_id=agent_a, conversation_id=conversation_id)
    assert bundle["AGENT:agent:rule"] == "always reply in Chinese"
    assert bundle["USER:profile:tone"] == "concise"
    assert bundle["CONVERSATION:conversation:" + str(conversation_id) + ":summary"] == "conv-summary"


def test_immutable_append_only_and_version_conflict():
    """immutable 键 append-only + 版本链；content_hash + version 冲突抛 AI_MEMORY_CONFLICT + audit。"""
    tenant_id, user_id, (agent_a, _), _ = _bootstrap()
    store = DbLongTermMemoryStore()
    with SessionLocal.begin() as db:
        first = store.remember_scoped(db, tenant_id=tenant_id, user_id=user_id, agent_id=agent_a, scope="AGENT", key="agent:immutable-fact", value="v1", immutable=True, source_ref="test:v1")
    assert first["version"] == 1 and first["appended"] is False

    with SessionLocal.begin() as db:
        second = store.remember_scoped(db, tenant_id=tenant_id, user_id=user_id, agent_id=agent_a, scope="AGENT", key="agent:immutable-fact", value="v2", immutable=True, source_ref="test:v2")
    assert second["version"] == 2 and second["appended"] is True

    with SessionLocal.begin() as db:
        with pytest.raises(ApiError) as exc:
            store.remember_scoped(db, tenant_id=tenant_id, user_id=user_id, agent_id=agent_a, scope="AGENT", key="agent:immutable-fact", value="v2", immutable=True, expected_version=1, source_ref="test:conflict")
    assert exc.value.code == "AI_MEMORY_CONFLICT"

    with SessionLocal() as db:
        active = db.scalars(select(LongMemory).where(LongMemory.tenant_id == tenant_id, LongMemory.key == "agent:immutable-fact", LongMemory.status == "ACTIVE")).all()
        superseded = db.scalars(select(LongMemory).where(LongMemory.tenant_id == tenant_id, LongMemory.key == "agent:immutable-fact", LongMemory.status == "SUPERSEDED")).all()
        versions = db.scalars(select(LongMemoryVersion).where(LongMemoryVersion.tenant_id == tenant_id)).all()
        conflicts = db.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == "MEMORY_CONFLICT")).all()
    assert len(active) == 1 and active[0].value == "v2" and active[0].version == 2
    assert len(superseded) == 1 and superseded[0].value == "v1"
    assert len(versions) >= 2
    assert len(conflicts) == 1


def test_soft_delete_hides_recall_and_audits():
    """soft-delete 后不可召回且 audit 有记录。"""
    tenant_id, user_id, (agent_a, _), _ = _bootstrap()
    store = DbLongTermMemoryStore()
    with SessionLocal.begin() as db:
        store.remember_scoped(db, tenant_id=tenant_id, user_id=user_id, agent_id=agent_a, scope="AGENT", key="agent:to-delete", value="secret", source_ref="test:del")
        assert store.forget_scoped(db, tenant_id=tenant_id, user_id=user_id, agent_id=agent_a, scope="AGENT", key="agent:to-delete", actor_user_id=user_id)
    with SessionLocal() as db:
        assert store.recall_scoped(db, tenant_id=tenant_id, user_id=user_id, agent_id=agent_a, scope="AGENT", key="agent:to-delete") is None
        audits = db.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == "MEMORY_DELETED")).all()
    assert len(audits) == 1
    assert audits[0].detail["key"] == "agent:to-delete"


@pytest.mark.asyncio
async def test_capabilities_reports_long_memory_persistent(monkeypatch):
    monkeypatch.setattr(get_settings(), "memory_long_enabled", True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/z-torque/capabilities")
    assert response.status_code == 200
    memory = response.json()["data"]["memory"]
    assert memory["long_persistent"] is True
    assert memory["long_scopes"] == ["CONVERSATION", "AGENT", "USER"]
