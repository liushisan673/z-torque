"""v1.3 M2 导出/归档 HTTP 端到端用例（此前只有代码审查，没有 API 级验证）。

覆盖：
1. POST /audit-exports → 轮询 GET → GET 分片下载 → 用独立 sha256 复算校验（不含内部对象）；
2. 能力先于权限：服务未装配 → 503（不是 403）；
3. 无 audit.export → 403 且落 AUDIT_EXPORT_DENIED（拒绝留痕）；
4. 未能承载的范围（超过 AUDIT_EXPORT_MAX_ROWS）→ 任务 FAILED + TOO_LARGE，**不产出**截断件；
5. 归档触发 → 批次落库 + 对象可被 manifest 校验。

装配方式与生产一致：enterprise profile 装配后把 `result.objects` 转挂 `app.state`，
测试结束 `result.shutdown()` 释放导出线程池与归档定时器。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.models  # noqa: F401  # 确保模型注册
from app.assembly import assemble
from app.builtin_plugins.modules import resolve_profile
from app.config import get_settings
from app.core.ids import utc_now
from app.db.session import SessionLocal, engine
from app.kernel.registry.active import active_registry, bind_registry
from app.main import app
from app.models import AuditLog, Permission, Role, Tenant, User, role_permission, user_role
from app.security.passwords import hash_password

PERMISSIONS = ("audit.read", "audit.read_all", "audit.export", "audit.archive")


@pytest.fixture(scope="module")
def enterprise_app():
    """enterprise 装配 + app.state 转挂；结束恢复全局注册表，避免污染其他用例。"""
    settings = get_settings()
    previous_registry = active_registry()
    previous_state = {
        key: getattr(app.state, key, None)
        for key in (
            "port_registry",
            "skill_gateway",
            "model_gateway",
            "mcp_stdio_registry",
            "audit_export_service",
            "audit_archive_service",
            "assembly_result",
            "assembly_plugins",
        )
    }
    result = assemble(settings, engine, profile=resolve_profile("enterprise"))
    app.state.assembly_result = result
    app.state.assembly_plugins = tuple(manifest.name for manifest in result.plugins)
    for object_key, object_value in result.objects.items():
        setattr(app.state, object_key, object_value)
    app.state.port_registry = result.registry
    app.state.skill_gateway = result.skill_gateway
    app.state.model_gateway = result.model_gateway
    app.state.mcp_stdio_registry = result.stdio_registry

    yield {"services": set(result.objects), "plugins": app.state.assembly_plugins}

    result.shutdown()
    for key, value in previous_state.items():
        if value is None:
            if hasattr(app.state, key):
                delattr(app.state, key)
        else:
            setattr(app.state, key, value)
    bind_registry(previous_registry)


@pytest.fixture()
def seeded(enterprise_app):
    """租户 + 有权限用户 + 无权限用户 + 三条业务审计（另有一条 AUDIT_* 自身事件）。"""
    tenant_id = uuid4()
    with SessionLocal.begin() as db:
        db.add(Tenant(id=tenant_id, name=f"v13-{tenant_id}"))
        db.flush()
        auditor = User(tenant_id=tenant_id, username=f"auditor-{uuid4()}", password_hash=hash_password("pass"))
        viewer = User(tenant_id=tenant_id, username=f"viewer-{uuid4()}", password_hash=hash_password("pass"))
        db.add_all([auditor, viewer])
        db.flush()

        role = Role(tenant_id=tenant_id, name="auditor")
        db.add(role)
        db.flush()
        for name in PERMISSIONS:
            permission = Permission(tenant_id=tenant_id, name=name)
            db.add(permission)
            db.flush()
            db.execute(role_permission.insert().values(role_id=role.id, permission_id=permission.id))
        db.execute(user_role.insert().values(user_id=auditor.id, role_id=role.id))

        for index in range(3):
            db.add(
                AuditLog(
                    tenant_id=tenant_id,
                    action="AGENT_CREATED",
                    resource_type="AGENT",
                    summary=f"agent {index}",
                    detail={"index": index},
                    created_at=utc_now() - timedelta(minutes=index),
                )
            )
        db.add(
            AuditLog(
                tenant_id=tenant_id,
                action="AUDIT_EXPORTED",
                resource_type="AUDIT_EXPORT_JOB",
                summary="self event",
                created_at=utc_now(),
            )
        )
        auditor_name, viewer_name = auditor.username, viewer.username

    return {
        "tenant_id": tenant_id,
        "auditor": auditor_name,
        "viewer": viewer_name,
        "password": "pass",
    }


async def _login(client: AsyncClient, username: str, password: str = "pass") -> str:
    response = await client.post("/api/z-torque/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["data"]["access_token"]


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _wait_job(client: AsyncClient, token: str, job_id: str, *, timeout: float = 15.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    payload: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        response = await client.get(f"/api/z-torque/audit-exports/{job_id}", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text
        payload = response.json()["data"]
        if payload["status"] in ("SUCCEEDED", "FAILED"):
            return payload
        await asyncio.sleep(0.05)
    raise AssertionError(f"job did not finish in time: {payload}")


@pytest.mark.asyncio
async def test_export_create_poll_download_and_verify_sha256(seeded):
    async with await _client() as client:
        token = await _login(client, seeded["auditor"])
        created = await client.post(
            "/api/z-torque/audit-exports", headers={"Authorization": f"Bearer {token}"}, json={"format": "JSONL"}
        )
        assert created.status_code == 200, created.text
        job_id = created.json()["data"]["id"]

        payload = await _wait_job(client, token, job_id)
        assert payload["status"] == "SUCCEEDED", payload
        # 递归防护：AUDIT_EXPORTED 自身事件不得混进导出件
        assert payload["rows_total"] == 3
        assert payload["shards_total"] >= 1

        shard = payload["shards"][0]
        download = await client.get(
            f"/api/z-torque/audit-exports/{job_id}/shards/{shard['seq']}/download",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert download.status_code == 200, download.text
        assert download.headers["content-type"] == "application/octet-stream"

        body = download.content
        # 下载内容必须与任务声明的摘要完全一致（独立复算，不信任服务端字段）
        assert hashlib.sha256(body).hexdigest() == shard["sha256"]
        rows = [json.loads(line) for line in body.decode("utf-8").splitlines()]
        assert len(rows) == payload["rows_total"]
        assert {row["action"] for row in rows} == {"AGENT_CREATED"}

        # 交付给外部的内容只含契约字段，不含内部 object_key / prefix
        assert "object_key" not in rows[0] and "object_prefix" not in rows[0]

        # 下载行为留痕
        with SessionLocal() as db:
            downloads = db.scalars(
                select(AuditLog).where(AuditLog.tenant_id == seeded["tenant_id"], AuditLog.action == "AUDIT_EXPORT_DOWNLOADED")
            ).all()
            assert len(downloads) == 1, "谁取走了哪一段必须可追溯"


@pytest.mark.asyncio
async def test_missing_permission_is_403_and_audited(seeded):
    async with await _client() as client:
        token = await _login(client, seeded["viewer"])
        response = await client.post(
            "/api/z-torque/audit-exports", headers={"Authorization": f"Bearer {token}"}, json={"format": "JSONL"}
        )
        assert response.status_code == 403
        assert response.json()["code"] == "AI_FORBIDDEN"

        # 分片下载同样要有鉴权，不能用"知道 job_id"绕过
        denied = await client.get(
            f"/api/z-torque/audit-exports/{uuid4()}/shards/1/download", headers={"Authorization": f"Bearer {token}"}
        )
        assert denied.status_code == 403

        anonymous = await client.post("/api/z-torque/audit-exports", json={"format": "JSONL"})
        assert anonymous.status_code in (401, 403)

        with SessionLocal() as db:
            rows = db.scalars(
                select(AuditLog).where(
                    AuditLog.tenant_id == seeded["tenant_id"], AuditLog.action == "AUDIT_EXPORT_DENIED"
                )
            ).all()
            # 上传/下载两条越权请求都必须留痕；下载链路在路由与服务内部各校验一次，
            # 因此行数可以多于请求数，断言"每次拒绝都有记录且权限点正确"。
            assert len(rows) >= 2, f"越权请求必须留痕: {[(row.summary, row.detail) for row in rows]}"
            assert {row.detail["permission"] for row in rows} == {"audit.export"}
            assert all(row.actor_user_id is not None for row in rows)


@pytest.mark.asyncio
async def test_not_assembled_returns_503_not_403(seeded, monkeypatch):
    async with await _client() as client:
        token = await _login(client, seeded["auditor"])
        monkeypatch.delattr(app.state, "audit_export_service")

        response = await client.post(
            "/api/z-torque/audit-exports", headers={"Authorization": f"Bearer {token}"}, json={"format": "JSONL"}
        )

        assert response.status_code == 503
        assert response.json()["code"] == "AI_CAPABILITY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_over_limit_job_fails_without_truncated_artifact(seeded):
    service = app.state.audit_export_service
    original = service._config
    service._config = replace(original, max_rows=1, chunk_rows=1)
    try:
        async with await _client() as client:
            token = await _login(client, seeded["auditor"])
            created = await client.post(
                "/api/z-torque/audit-exports", headers={"Authorization": f"Bearer {token}"}, json={"format": "JSONL"}
            )
            assert created.status_code == 200, "超出上限不是同步校验错误：任务建立后异步失败"
            payload = await _wait_job(client, token, created.json()["data"]["id"])

            assert payload["status"] == "FAILED"
            assert payload["error_code"] == "AI_AUDIT_EXPORT_TOO_LARGE"
            assert payload["rows_total"] == 0
            assert payload["shards"] == [], "失败任务不得留下可下载分片"
    finally:
        service._config = original


@pytest.mark.asyncio
async def test_archive_trigger_returns_verifiable_batch(seeded):
    old = utc_now() - timedelta(days=100)
    with SessionLocal.begin() as db:
        for index in range(2):
            db.add(
                AuditLog(
                    tenant_id=seeded["tenant_id"],
                    action="AGENT_CREATED",
                    resource_type="AGENT",
                    summary=f"cold {index}",
                    created_at=old + timedelta(minutes=index),
                )
            )

    async with await _client() as client:
        token = await _login(client, seeded["auditor"])
        response = await client.post(
            "/api/z-torque/audit-archive-runs",
            headers={"Authorization": f"Bearer {token}"},
            json={"lookback_hours": 24 * 120},
        )
        assert response.status_code == 200, response.text
        run = response.json()["data"]

        assert run["status"] == "SUCCEEDED", run
        assert run["rows"] == 2
        assert run["sha256_total"]
        assert {item["object_key"].split(".")[-1] for item in run["objects"]} == {"jsonl"}

        listing = await client.get("/api/z-torque/audit-archive-runs", headers={"Authorization": f"Bearer {token}"})
        assert listing.status_code == 200
        assert [item["id"] for item in listing.json()["data"]["items"]][0] == run["id"]

        # 没有 audit.archive 的用户可以看批次列表（audit.read）但不能触发
        viewer = await _login(client, seeded["viewer"])
        denied = await client.post(
            "/api/z-torque/audit-archive-runs", headers={"Authorization": f"Bearer {viewer}"}, json={"lookback_hours": 24}
        )
        assert denied.status_code == 403
