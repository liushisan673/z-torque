"""P2-14 ~ P2-19 契约缺口与健壮性回归测试。"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.assembly import _apply_port_overrides
from app.builtin_plugins.authz_provider import BuiltinAuthzProvider
from app.builtin_plugins.manifest import PluginManifest, PluginSdk
from app.builtin_plugins.notify_channel import LoggingNotifyChannel
from app.builtin_plugins.sandbox_driver import run_isolated_process
from app.config import get_settings
from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.kernel.audit.service import record_audit
from app.kernel.md_skill import parse_skill
from app.kernel.registry.active import active_registry, bind_registry, unbind_registry
from app.kernel.registry.port_registry import PORT_SPEC_BY_NAME, PortPriority, PortRegistry
from app.kernel.security_context import SecurityContext
from app.kernel.task.service import create_task, transition_task
from app.kernel.task.states import TaskStatus
from app.main import app
from app.models import Agent, AgentVersion, Conversation, Message, Tenant, User
from app.models.audit import AuditLog
from app.models.task import Task, TaskEvent
from app.services.context_builder import build_context
from app.services.md_skill_upload import peek_skill_name

_CREATED_TENANTS: list = []


@pytest.fixture(autouse=True)
def cleanup():
    _CREATED_TENANTS.clear()
    yield
    with SessionLocal.begin() as db:
        if _CREATED_TENANTS:
            # 只清理本文件创建的租户数据，避免影响其它用例依赖的种子数据
            for model in (AuditLog, TaskEvent, Task, Message, Conversation, AgentVersion, Agent, User):
                db.execute(delete(model).where(model.tenant_id.in_(_CREATED_TENANTS)))
            db.execute(delete(Tenant).where(Tenant.id.in_(_CREATED_TENANTS)))
    _CREATED_TENANTS.clear()


def _bootstrap():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"p2-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username=f"p2-{uuid4()}", password_hash="x")
        db.add(user)
        db.flush()
        agent = Agent(
            tenant_id=tenant.id,
            owner_user_id=None,
            name="P2 Agent",
            description="p2",
            source_type="OFFICIAL",
            status="ACTIVE",
            is_default_parent=True,
        )
        db.add(agent)
        db.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version_number=1,
            status="PUBLISHED",
            config_schema_version="1",
            config_snapshot={"mode": "mock"},
        )
        db.add(version)
        db.flush()
        _CREATED_TENANTS.append(tenant.id)
        return tenant.id, user.id, agent.id, version.id


# ---------------- P2-14：Port 真实调用缝 ----------------


class _StaticPolicy:
    def user_permissions(self, db, *, user_id):
        return {"chat:use"}


def test_authz_provider_reads_policy_source_port():
    """policy.source 成为权限来源：无需 DB 角色行也能判定。"""
    provider = BuiltinAuthzProvider(_StaticPolicy())
    assert provider.has_all(None, user_id=uuid4(), required=["chat:use"]) is True
    assert provider.has_all(None, user_id=uuid4(), required=["chat:use", "admin:write"]) is False


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))


class _StubRegistry:
    def __init__(self, sink: _RecordingSink) -> None:
        self._sink = sink

    def try_get(self, name: str):
        return self._sink if name == "observability.sink" else None

    def get(self, name: str):
        return self._sink


def test_transition_emits_observability_event():
    """observability.sink 成为状态机外发事件的真实调用方。"""
    tenant_id, user_id, agent_id, version_id = _bootstrap()
    with SessionLocal.begin() as db:
        task = create_task(
            db,
            ctx=SecurityContext(tenant_id=tenant_id, user_id=user_id),
            agent_id=agent_id,
            agent_version_id=version_id,
            idempotency_key=str(uuid4()),
            request_summary="emit",
        )
        task_id = task.id

    sink = _RecordingSink()
    previous = active_registry()
    bind_registry(_StubRegistry(sink))
    try:
        with SessionLocal.begin() as db:
            task = db.get(Task, task_id)
            transition_task(db, task, TaskStatus.QUEUED, "TASK_QUEUED", "queued")
    finally:
        if previous is not None:
            bind_registry(previous)
        else:
            unbind_registry()

    assert any(event == "task.transition" and payload["task_id"] == str(task_id) for event, payload in sink.events)


def test_build_context_uses_working_memory_and_long_memory(monkeypatch):
    """memory.working 缓存上下文快照；memory.long 在开关打开时召回摘要。"""
    tenant_id, user_id, agent_id, _ = _bootstrap()
    with SessionLocal.begin() as db:
        conversation = Conversation(tenant_id=tenant_id, owner_user_id=user_id, agent_id=agent_id, title="p2")
        db.add(conversation)
        db.flush()
        db.add(Message(tenant_id=tenant_id, conversation_id=conversation.id, sender_type="USER", content="hello"))
        conversation_id = conversation.id

    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        assert conversation is not None
        first = build_context(db, conversation)
        snapshot = app.state.port_registry.get("memory.working").get(tenant_id, user_id, conversation_id)
        second = build_context(db, conversation)
    assert snapshot is not None and "fingerprint" in snapshot
    assert [item.content for item in first] == [item.content for item in second]
    assert any(item.content == "hello" for item in second)

    monkeypatch.setattr(get_settings(), "memory_long_enabled", True)
    long_memory = app.state.port_registry.get("memory.long")
    long_memory.remember(tenant_id=tenant_id, user_id=user_id, key=f"conversation:{conversation_id}:summary", value="LONG-SUMMARY")
    try:
        with SessionLocal() as db:
            conversation = db.get(Conversation, conversation_id)
            assert conversation is not None
            recalled = build_context(db, conversation)
    finally:
        long_memory.forget(tenant_id=tenant_id, user_id=user_id, key=f"conversation:{conversation_id}:summary")
    assert any("LONG-SUMMARY" in item.content for item in recalled)


# ---------------- P2-15：显式配置 / 插件优先级 ----------------


class _FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None]] = []

    def register(self, port_name, factory, *, priority, label, plugin=None):
        self.calls.append((port_name, priority, plugin))


def test_plugin_sdk_defaults_to_plugin_priority_for_external_plugins():
    manifest = PluginManifest(name="ext", description="", provides=("memory.long",))
    external = _FakeRegistry()
    PluginSdk(registry=external, manifest=manifest, builtin=False).register_port("memory.long", lambda: None, label="ext")
    assert external.calls[0][1] == PortPriority.PLUGIN

    builtin = _FakeRegistry()
    PluginSdk(registry=builtin, manifest=manifest, builtin=True).register_port("memory.long", lambda: None, label="builtin")
    assert builtin.calls[0][1] == PortPriority.BUILTIN


class _OverrideSettings:
    port_override_map = {"notify.channel": "app.builtin_plugins.notify_channel:LoggingNotifyChannel"}


def test_explicit_port_override_wins_over_builtin():
    registry = PortRegistry(specs=(PORT_SPEC_BY_NAME["notify.channel"],))
    registry.register("notify.channel", lambda: object(), label="builtin")
    _apply_port_overrides(registry, _OverrideSettings())
    registry.resolve()
    assert isinstance(registry.get("notify.channel"), LoggingNotifyChannel)


def test_invalid_port_override_is_rejected():
    registry = PortRegistry(specs=(PORT_SPEC_BY_NAME["notify.channel"],))

    class _Bad:
        port_override_map = {"notify.channel": "not-a-module-path"}

    with pytest.raises(Exception):
        _apply_port_overrides(registry, _Bad())


# ---------------- P2-16：SAVEPOINT 隔离 ----------------


def test_failed_transition_keeps_caller_audit_writes():
    """并发失败只回滚 transition 自身写入，不丢调用方已 flush 的审计。"""
    tenant_id, user_id, agent_id, version_id = _bootstrap()
    with SessionLocal.begin() as db:
        task = create_task(
            db,
            ctx=SecurityContext(tenant_id=tenant_id, user_id=user_id),
            agent_id=agent_id,
            agent_version_id=version_id,
            idempotency_key=str(uuid4()),
            request_summary="savepoint",
        )
        task_id = task.id

    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task is not None
        task.status = TaskStatus.CANCELLED
        db.flush()
        record_audit(
            db,
            tenant_id=tenant_id,
            actor_user_id=user_id,
            action="P2_SAVEPOINT_PROBE",
            resource_type="TASK",
            resource_id=task_id,
            summary="must survive",
        )
        with pytest.raises(ApiError):
            transition_task(db, task, TaskStatus.RUNNING, "TASK_RUNNING", "run")
        db.commit()

    with SessionLocal() as db:
        assert db.scalar(select(AuditLog.id).where(AuditLog.action == "P2_SAVEPOINT_PROBE")) is not None
        assert db.scalar(select(TaskEvent.id).where(TaskEvent.task_id == task_id, TaskEvent.event_type == "TASK_RUNNING")) is None


# ---------------- P2-17：健康检查 / trace / 配置强度 ----------------


@pytest.mark.asyncio
async def test_health_returns_503_when_database_unavailable(monkeypatch):
    def _boom():
        raise RuntimeError("database down")

    monkeypatch.setattr("app.main.check_database", _boom)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 503
    assert response.json()["data"]["database"] is False


@pytest.mark.asyncio
async def test_trace_id_header_is_validated():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        valid = await client.get("/health", headers={"X-Trace-ID": "client-trace-123"})
        invalid = await client.get("/health", headers={"X-Trace-ID": "x" * 100})
    assert valid.headers["X-Trace-ID"] == "client-trace-123"
    regenerated = invalid.headers["X-Trace-ID"]
    assert regenerated != "x" * 100
    assert len(regenerated) == 36


def test_production_rejects_weak_admin_password_and_jwt_secret():
    base = get_settings()
    weak_password = base.model_copy(update={"app_env": "production", "admin_initial_password": "admin123"})
    with pytest.raises(ValueError):
        weak_password.validate_runtime()

    weak_jwt = base.model_copy(update={"app_env": "production", "admin_initial_password": "Str0ng-Passw0rd-For-P2!", "jwt_secret_key": "short"})
    with pytest.raises(ValueError):
        weak_jwt.validate_runtime()


# ---------------- P2-18：CRLF front matter / 脚本输出 ----------------


def test_crlf_front_matter_is_accepted(tmp_path):
    assert peek_skill_name("---\r\nname: demo-skill\r\n---\r\nbody") == "demo-skill"

    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_bytes(
        b"---\r\n"
        b"name: demo-skill\r\n"
        b"description: demo\r\n"
        b"implementation: md_instruction\r\n"
        b"version: 1\r\n"
        b"input_schema: {type: object}\r\n"
        b"output_schema: {type: object}\r\n"
        b"allow_scripts: false\r\n"
        b"required_permissions: []\r\n"
        b"---\r\n"
        b"do the thing\r\n"
    )
    manifest = parse_skill(tmp_path, skill_dir)
    assert manifest.name == "demo-skill"


def test_script_non_json_output_raises_api_error(tmp_path):
    script = tmp_path / "skill.py"
    script.write_text("print('not json at all')", encoding="utf-8")
    with pytest.raises(ApiError) as exc_info:
        asyncio.run(
            run_isolated_process(
                script_path=script,
                arguments={},
                cwd=tmp_path,
                timeout_seconds=10,
                max_output_bytes=10000,
            )
        )
    assert exc_info.value.code == "AI_SKILL_FAILED"
