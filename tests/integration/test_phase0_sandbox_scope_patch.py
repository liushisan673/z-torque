"""Phase 0 §2.3 真实入口验收：显式 Scope / SkillGateway 唯一入口 / PatchTransaction。

验收全部走真实入口：HTTP /workspace/patch、SkillGateway.execute_async、
SandboxDriver.execute；不靠 mock 替代被测逻辑。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.builtin_plugins.sandbox_driver.process import ProcessSandboxDriver
from app.config import get_settings
from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.kernel.md_skill import MdSkillManifest
from app.kernel.md_skill_runtime import MdSkillRuntime
from app.kernel.ports.sandbox_driver import SANDBOX_MODE_FULL, SandboxScope
from app.kernel.security_context import SecurityContext
from app.kernel.skill_gateway import SkillGateway
from app.kernel.task.service import create_task, transition_task
from app.kernel.task.states import TaskStatus
from app.main import app
from app.models import (
    Agent,
    AgentVersion,
    AuditLog,
    AuthSession,
    Confirmation,
    Message,
    Outbox,
    PatchArtifact,
    Skill,
    SkillVersion,
    Task,
    TaskCheckpoint,
    TaskEvent,
    Tenant,
    User,
)
from app.security.passwords import hash_password
from tests.support.db_cleanup import cleanup_session


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with cleanup_session() as db:
        for model in (
            PatchArtifact, Message, TaskEvent, Outbox, TaskCheckpoint, Confirmation,
            AuditLog, Task, SkillVersion, Skill, AgentVersion, Agent, AuthSession, User, Tenant,
        ):
            db.execute(delete(model))
    settings = get_settings()
    for path in Path(settings.md_skills_dir).glob(f"phase0-patch-{uuid4().hex}*"):
        path.unlink(missing_ok=True)
    for path in Path(settings.md_skills_dir).glob("phase0-patch-*"):
        path.unlink(missing_ok=True)


def _bootstrap():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"scope-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        agent = Agent(tenant_id=tenant.id, owner_user_id=None, name="A", source_type="OFFICIAL", status="ACTIVE")
        db.add(agent)
        db.flush()
        version = AgentVersion(
            tenant_id=tenant.id, agent_id=agent.id, version_number=1, status="PUBLISHED",
            config_schema_version="1", config_snapshot={"mode": "mock"},
        )
        db.add(version)
        db.flush()
        agent.current_published_version_id = version.id
        return tenant.id, user.id, agent.id, version.id


def _username(user_id) -> str:
    with SessionLocal() as db:
        return db.get(User, user_id).username


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _skills_root() -> Path:
    return Path(get_settings().md_skills_dir).resolve()


@pytest.mark.asyncio
async def test_capabilities_reports_explicit_scope_and_gateway_only_entry():
    """真实入口：GET /capabilities 如实上报 scope/gate/不降级语义。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/z-torque/capabilities")
    assert response.status_code == 200
    sandbox = response.json()["data"]["sandbox"]
    assert sandbox["scope"]["explicit"] is True
    assert sandbox["scope"]["gateway_only"] is True
    assert sandbox["scope"]["full_requires_container"] is True
    assert sandbox["scope"]["modes"] == ["restricted", "full"]
    assert sandbox["patch"]["available"] is True
    assert sandbox["untrusted_execution"] == "unavailable"  # 默认 process，不报可用


@pytest.mark.asyncio
async def test_patch_via_http_entry_commits_with_artifact_and_metadata_only_audit():
    """真实入口：登录 → POST /workspace/patch → 文件变更 + artifact + metadata-only 审计。"""
    tenant_id, user_id, _, _ = _bootstrap()
    root = _skills_root()
    target = root / f"phase0-patch-{uuid4().hex}.txt"
    original = "before-value\n"
    target.write_bytes(original.encode("utf-8"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/z-torque/auth/login", json={"username": _username(user_id), "password": "pass"})
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        response = await client.post(
            "/api/z-torque/workspace/patch", headers=headers,
            json={"base_dir": str(root), "patches": [{"path": str(target), "old_text": "before-value", "new_text": "after-value", "expected_hash": _sha(original)}]},
        )
    assert response.status_code == 200, response.text
    assert target.read_bytes() == b"after-value\n"
    payload = response.json()["data"]
    assert payload["status"] == "COMMITTED"
    with SessionLocal() as db:
        artifact = db.scalar(select(PatchArtifact).where(PatchArtifact.path == str(target)))
        assert artifact is not None and artifact.before_text == original
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "PATCH_COMMIT"))
        assert audit is not None
        assert audit.detail["artifact_id"] == str(artifact.id)
        assert "before_text" not in audit.detail and "after_text" not in audit.detail


@pytest.mark.asyncio
async def test_patch_conflict_rolls_back_without_residue():
    """真实入口：提交前目标被并发修改 → 409 AI_PATCH_CONFLICT，文件无补丁残留。"""
    tenant_id, user_id, _, _ = _bootstrap()
    root = _skills_root()
    target = root / f"phase0-patch-{uuid4().hex}.txt"
    original = "original\n"
    target.write_bytes(original.encode("utf-8"))
    stale_hash = _sha(original)
    target.write_bytes(b"concurrent-write\n")  # 审批/读 hash 之后被并发修改
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/z-torque/auth/login", json={"username": _username(user_id), "password": "pass"})
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        response = await client.post(
            "/api/z-torque/workspace/patch", headers=headers,
            json={"base_dir": str(root), "patches": [{"path": str(target), "old_text": "original", "new_text": "patched", "expected_hash": stale_hash}]},
        )
    assert response.status_code == 409
    assert response.json()["code"] == "AI_PATCH_CONFLICT"
    assert target.read_bytes() == b"concurrent-write\n"
    with SessionLocal() as db:
        assert db.scalar(select(PatchArtifact).where(PatchArtifact.path == str(target))) is None
        assert db.scalar(select(AuditLog).where(AuditLog.action == "PATCH_CONFLICT")) is not None


def _make_skill_version(db, tenant_id, *, declared_permissions: list[str], required_permissions: list[str], relative_path: str | None = None):
    skill = Skill(tenant_id=tenant_id, name=f"scope-{uuid4().hex}", status="ACTIVE", description="scope test")
    db.add(skill)
    db.flush()
    version = SkillVersion(
        tenant_id=tenant_id, skill_id=skill.id, version_number=1, status="PUBLISHED",
        required_permissions=required_permissions,
        config_snapshot={
            "name": skill.name, "description": "scope test", "implementation": "md_script",
            "declared_version": 1, "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
            "allow_scripts": True, "required_permissions": declared_permissions,
            "instructions": "", "script_files": ["scripts/demo.py"], "file_fingerprint": "x" * 64,
            "relative_path": relative_path or skill.name, "windows_trusted": False,
        },
    )
    db.add(version)
    db.flush()
    return version


@pytest.mark.asyncio
async def test_gate_denies_full_scope_without_container_and_audits():
    """真实入口：manifest 声明 sandbox.full 但无容器隔离 → 显式不可用，不静默降级。"""
    tenant_id, user_id, _, _ = _bootstrap()
    with SessionLocal.begin() as db:
        version = _make_skill_version(db, tenant_id, declared_permissions=["sandbox.full"], required_permissions=[])
    gateway = SkillGateway()
    with SessionLocal() as db:
        result = await gateway.execute_async(
            db, SecurityContext(tenant_id=tenant_id, user_id=user_id),
            skill_version_id=version.id, arguments={},
        )
    assert result.status == "FAILED"
    assert result.error_code == "AI_CAPABILITY_UNAVAILABLE"
    with SessionLocal() as db:
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "SANDBOX_DENIED"))
        assert audit is not None
        assert audit.detail["code"] == "AI_CAPABILITY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_gate_denies_out_of_scope_script_path_and_audits():
    """真实入口：越权脚本路径经 SkillGateway gate 拒绝，audit 有 sandbox.deny 记录。"""
    tenant_id, user_id, _, _ = _bootstrap()
    with SessionLocal.begin() as db:
        version = _make_skill_version(
            db, tenant_id, declared_permissions=[], required_permissions=[],
            relative_path="../outside-scope",
        )
    gateway = SkillGateway()
    with SessionLocal() as db:
        result = await gateway.execute_async(
            db, SecurityContext(tenant_id=tenant_id, user_id=user_id),
            skill_version_id=version.id, arguments={},
        )
    assert result.status == "FAILED"
    assert result.error_code == "AI_SANDBOX_DENIED"
    with SessionLocal() as db:
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "SANDBOX_DENIED"))
        assert audit is not None
        assert audit.detail["code"] == "AI_SANDBOX_DENIED"


@pytest.mark.asyncio
async def test_approved_scope_tampering_is_rejected_at_execution_point():
    """真实入口：审批 checkpoint 中 scope 被改 → 执行点二次校验拒绝 + 审计。"""
    tenant_id, user_id, agent_id, version_id = _bootstrap()
    with SessionLocal.begin() as db:
        skill_version = _make_skill_version(db, tenant_id, declared_permissions=[], required_permissions=[])
        task = create_task(
            db, ctx=SecurityContext(tenant_id=tenant_id, user_id=user_id),
            agent_id=agent_id, agent_version_id=version_id, idempotency_key=str(uuid4()),
            request_summary="scope tamper",
        )
        transition_task(db, task, TaskStatus.QUEUED, "TASK_QUEUED", "test")
        transition_task(db, task, TaskStatus.RUNNING, "TASK_CLAIMED", "test")
        transition_task(db, task, TaskStatus.WAITING_HUMAN, "TASK_WAITING_HUMAN", "approval")
        confirmation = Confirmation(
            tenant_id=tenant_id, requested_by_user_id=user_id, reason="test",
            request_snapshot={}, task_id=task.id, skill_version_id=skill_version.id, status="APPROVED",
        )
        db.add(confirmation)
        db.flush()
        checkpoint = TaskCheckpoint(
            tenant_id=tenant_id, task_id=task.id, status="APPROVED", confirmation_id=confirmation.id,
            execution_context={"pending_call_hash": "x", "skill_version_id": str(skill_version.id)},
            completed_steps=[], pending_steps=["md_script"], idempotency_key=str(uuid4()),
            pending_call={"skill_version_id": str(skill_version.id), "arguments": {}, "implementation": "md_script",
                          "sandbox_scope": {"allowed_roots": ["/tmp/tampered"], "mode": "full", "isolation": "container"}},
        )
        db.add(checkpoint)
        db.flush()
        checkpoint_id, task_id = checkpoint.id, task.id
    gateway = SkillGateway()
    with SessionLocal() as db:
        result = await gateway.execute_async(
            db, SecurityContext(tenant_id=tenant_id, user_id=user_id),
            skill_version_id=skill_version.id, arguments={}, task_id=task_id,
            resume_checkpoint_id=checkpoint_id,
        )
    assert result.status == "FAILED"
    assert result.error_code == "AI_SANDBOX_DENIED"
    with SessionLocal() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "SANDBOX_DENIED")) is not None


@pytest.mark.asyncio
async def test_full_scope_never_silently_downgrades_to_process(tmp_path):
    """真实入口：full scope 落到 process 驱动显式 AI_CAPABILITY_UNAVAILABLE。"""
    root = tmp_path
    skill_root = root / "demo"
    script = skill_root / "scripts" / "demo.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('ok')", encoding="utf-8")
    driver = ProcessSandboxDriver(allowed_roots=[str(root)])
    full_scope = SandboxScope((str(root),), mode=SANDBOX_MODE_FULL, isolation="process")
    runtime = MdSkillRuntime(root, enabled=True, sandbox_driver=driver, allowed_roots=[str(root)])
    manifest = MdSkillManifest(
        name="demo", description="demo", implementation="md_script", declared_version=1,
        input_schema={"type": "object"}, output_schema={"type": "object"}, allow_scripts=True,
        required_permissions=[], instructions="", script_files=["scripts/demo.py"],
        file_fingerprint="x" * 64, relative_path="demo",
    )
    with pytest.raises(ApiError) as exc:
        await runtime.execute(manifest, {}, scope=full_scope, approval_granted=True)
    assert exc.value.code == "AI_CAPABILITY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_bounded_to_thread_confirms_exit_on_timeout():
    """清理路径：超时后不再留下仍在运行的 to_thread 任务。"""
    import asyncio
    import time

    from app.builtin_plugins.sandbox_driver.container import _bounded_to_thread

    entered = asyncio.Event()

    def slow():
        entered.set()
        time.sleep(0.05)
        return "done"

    with pytest.raises(asyncio.TimeoutError):
        await _bounded_to_thread(slow, timeout=0.001)
    assert entered.is_set()
    await asyncio.sleep(0.06)
    assert not [task for task in asyncio.all_tasks() if task.get_coro().__qualname__.startswith("to_thread")]
