"""Phase 2 阶段四：统一本机调度器（interval + cron）真实入口验收。

负向先行：非法 interval / cron 必须拒绝；失败任务隔离；重复注册拒绝；
正向验证周期执行、手动触发、watcher 托管、capabilities 与受控 API。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.api.routes.scheduler import router as scheduler_router
from app.core.errors import SCHEDULER_ERROR_CODES, ApiError
from app.db.session import SessionLocal
from app.kernel.local_scheduler import LocalScheduler, ScheduleSpec, next_cron_delay, parse_cron
from app.kernel.md_skill_watcher import MdSkillWatcher
from app.main import app
from app.models import Permission, Role, Tenant, User, role_permission, user_role
from app.security.passwords import hash_password

_CREATED_TENANTS: list = []


@pytest.fixture(autouse=True)
def cleanup():
    _CREATED_TENANTS.clear()
    yield
    with SessionLocal.begin() as db:
        if _CREATED_TENANTS:
            role_ids = select(Role.id).where(Role.tenant_id.in_(_CREATED_TENANTS))
            db.execute(delete(user_role).where(user_role.c.user_id.in_(
                select(User.id).where(User.tenant_id.in_(_CREATED_TENANTS))
            )))
            db.execute(delete(role_permission).where(role_permission.c.role_id.in_(role_ids)))
            db.execute(delete(Role).where(Role.tenant_id.in_(_CREATED_TENANTS)))
            db.execute(delete(Permission).where(Permission.tenant_id.in_(_CREATED_TENANTS)))
            db.execute(delete(User).where(User.tenant_id.in_(_CREATED_TENANTS)))
            db.execute(delete(Tenant).where(Tenant.id.in_(_CREATED_TENANTS)))
    _CREATED_TENANTS.clear()


def _bootstrap_admin_username() -> str:
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"sched-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(
            tenant_id=tenant.id, username=f"admin-{uuid4()}",
            password_hash=hash_password("pass"),
        )
        db.add(user)
        db.flush()
        role = Role(tenant_id=tenant.id, name="admin")
        db.add(role)
        db.flush()
        permission = Permission(tenant_id=tenant.id, name="system.config")
        db.add(permission)
        db.flush()
        db.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
        db.execute(role_permission.insert().values(role_id=role.id, permission_id=permission.id))
        _CREATED_TENANTS.append(tenant.id)
        return user.username


def test_schedule_spec_rejects_invalid_definitions():
    """负向：二选一周期、正数间隔、合法 cron 表达式。"""
    for kwargs in (
        {},
        {"interval_seconds": 1, "cron": "0 0 * * *"},
        {"interval_seconds": 0},
        {"cron": "not a cron"},
        {"cron": "61 * * * *"},
    ):
        with pytest.raises(ApiError) as exc:
            ScheduleSpec(name="bad", callback=lambda: None, **kwargs)
        assert exc.value.code in SCHEDULER_ERROR_CODES


@pytest.mark.asyncio
async def test_interval_jobs_run_and_status_tracks_runs():
    counter: list[int] = []
    scheduler = LocalScheduler()
    scheduler.register(ScheduleSpec(
        name="tick", callback=lambda: counter.append(1), interval_seconds=0.02,
    ))
    await scheduler.start()
    await asyncio.sleep(0.12)
    await scheduler.stop()
    assert len(counter) >= 2
    status = scheduler.status()[0]
    assert status["runs"] >= 2 and status["failures"] == 0
    assert status["running"] is False


@pytest.mark.asyncio
async def test_failing_job_is_isolated_from_healthy_jobs():
    healthy: list[int] = []

    def failing():
        raise RuntimeError("boom")

    scheduler = LocalScheduler()
    scheduler.register(ScheduleSpec(name="bad", callback=failing, interval_seconds=0.02))
    scheduler.register(ScheduleSpec(name="good", callback=lambda: healthy.append(1), interval_seconds=0.02))
    await scheduler.start()
    await asyncio.sleep(0.12)
    await scheduler.stop()
    assert healthy, "健康任务不得被失败任务拖停"
    bad = next(item for item in scheduler.status() if item["name"] == "bad")
    assert bad["failures"] >= 1 and bad["runs"] == 0
    assert bad["last_error"] == "boom"


def test_cron_parsing_and_next_delay():
    minute, hour, day, month, weekday = parse_cron("*/5 * * * *")
    assert len(minute) == 12 and 0 in minute and 5 in minute
    assert hour == set(range(24)) and weekday == set(range(7))
    now = datetime(2026, 11, 24, 10, 3, 30, tzinfo=timezone.utc)
    assert next_cron_delay("*/5 * * * *", now) == pytest.approx(90.0, abs=0.01)


@pytest.mark.asyncio
async def test_manual_trigger_and_duplicate_registration():
    calls: list[str] = []
    scheduler = LocalScheduler()
    scheduler.register(ScheduleSpec(name="manual", callback=lambda: calls.append("ok"), interval_seconds=60))
    with pytest.raises(ApiError) as exc:
        scheduler.register(ScheduleSpec(name="manual", callback=lambda: None, interval_seconds=60))
    assert exc.value.code == "AI_SCHEDULE_DUPLICATE"
    with pytest.raises(ApiError) as exc:
        await scheduler.trigger("missing")
    assert exc.value.code == "AI_SCHEDULE_NOT_FOUND"
    await scheduler.trigger("manual")
    assert calls == ["ok"]
    assert scheduler.status()[0]["runs"] == 1


@pytest.mark.asyncio
async def test_watcher_delegates_periodic_scan_to_scheduler(tmp_path):
    scheduler = LocalScheduler()
    watcher = MdSkillWatcher(tmp_path, uuid4(), interval_seconds=1.0, scheduler=scheduler)

    async def fake_scan():
        return None

    watcher.scan = fake_scan  # type: ignore[method-assign]
    await watcher.start()
    names = [item["name"] for item in scheduler.status()]
    assert any(name.startswith("md_skill_watch:") for name in names)
    await scheduler.stop()
    await watcher.stop()
    assert not any(item["running"] for item in scheduler.status())


@pytest.mark.asyncio
async def test_capabilities_and_controlled_api_expose_scheduler():
    assert any(route.path.endswith("/scheduler/status") for route in scheduler_router.routes)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        capabilities = await client.get("/api/z-torque/capabilities")
        assert capabilities.status_code == 200
        scheduler_capability = capabilities.json()["data"]["scheduler"]
        assert scheduler_capability["available"] is True
        assert scheduler_capability["interval"] is True and scheduler_capability["cron"] is True

        login = await client.post(
            "/api/z-torque/auth/login",
            json={"username": _bootstrap_admin_username(), "password": "pass"},
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        status = await client.get("/api/z-torque/scheduler/status", headers=headers)
        assert status.status_code == 200, status.text
        assert isinstance(status.json()["data"], list)

        missing = await client.post("/api/z-torque/scheduler/does-not-exist/trigger", headers=headers)
        assert missing.status_code == 404
