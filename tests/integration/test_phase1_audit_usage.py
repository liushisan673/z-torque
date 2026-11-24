"""Phase 1 §三-3：审计字段收紧 + usage 归一化真实入口验收。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.builtin_plugins.model_gateway import OpenAICompatibleGateway
from app.db.session import SessionLocal
from app.kernel.audit.service import record_audit
from app.kernel.model_gateway.contracts import ModelUsage, normalize_usage, parse_model_usage
from app.main import app
from app.models import Agent, AuditLog, Tenant, UsageSummary, User
from app.security.passwords import hash_password
from app.services.usage_service import record_usage_summary
from tests.support.ddl import fk_off, fk_on

TRACE = "phase1-audit-usage"


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with SessionLocal.begin() as db:
        fk_off(db)
        for model in (UsageSummary, AuditLog, Agent, User, Tenant):
            db.execute(delete(model))
        fk_on(db)


def _bootstrap():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"usage-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        agent = Agent(tenant_id=tenant.id, owner_user_id=user.id, name="A", source_type="USER", status="ACTIVE")
        db.add(agent)
        db.flush()
        return tenant.id, user.id, user.username, agent.id


async def _login(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post("/api/z-torque/auth/login", json={"username": username, "password": "pass"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def test_usage_normalization_marks_missing_tokens_unknown():
    known = normalize_usage(parse_model_usage({"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}))
    assert known["prompt_tokens"] == 12
    assert known["completion_tokens"] == 3
    assert known["total_tokens"] == 15
    assert known["cache_read_tokens"] == "unknown"
    assert known["cache_write_tokens"] == "unknown"

    unknown = normalize_usage(ModelUsage())
    assert set(unknown.values()) == {"unknown"}
    assert 0 not in unknown.values()


def test_openai_gateway_absent_usage_is_unknown():
    response = OpenAICompatibleGateway._normalize(
        {"id": "resp-1", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    )
    assert response.usage is not None
    assert set(normalize_usage(response.usage).values()) == {"unknown"}


@pytest.mark.asyncio
async def test_audit_detail_is_metadata_only_at_real_api_entry():
    tenant_id, user_id, username, _agent_id = _bootstrap()
    with SessionLocal.begin() as db:
        record_audit(
            db,
            tenant_id=tenant_id,
            actor_user_id=user_id,
            action="MODEL_CALL_COMPLETED",
            resource_type="TASK",
            resource_id=None,
            summary="model call completed",
            detail={
                "status": "SUCCEEDED",
                "error_code": None,
                "tool_version_id": str(uuid4()),
                "query_hash": "a" * 64,
                "prompt": "请勿外泄的系统提示词",
                "messages": [{"role": "user", "content": "用户正文"}],
                "arguments": {"password": "plain"},
                "api_key": "secret-value",
                "before_text": "old document body",
                "after_text": "new document body",
                "usage": normalize_usage(ModelUsage()),
            },
            trace_id=TRACE,
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, username)
        response = await client.get(f"/api/z-torque/audit/by-trace/{TRACE}", headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    events = payload["items"]
    assert len(events) == 1
    detail = events[0]["detail"]
    for banned in ("prompt", "messages", "arguments", "api_key", "before_text", "after_text"):
        assert banned not in detail
    assert detail["query_hash"] == "a" * 64
    assert detail["usage"]["prompt_tokens"] == "unknown"
    assert detail["usage"]["cache_read_tokens"] == "unknown"


@pytest.mark.asyncio
async def test_usage_summary_api_uses_unknown_not_zero():
    tenant_id, _user_id, username, agent_id = _bootstrap()
    with SessionLocal.begin() as db:
        record_usage_summary(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            model="known-model",
            usage=parse_model_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
            window="2026-11-24",
        )
        record_usage_summary(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            model="unknown-model",
            usage=ModelUsage(),
            window="2026-11-24",
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, username)
        response = await client.get("/api/z-torque/usage/summary", headers=headers)
    assert response.status_code == 200, response.text
    rows = {row["model"]: row for row in response.json()["data"]}
    assert rows["known-model"]["token_in"] == 10
    assert rows["known-model"]["token_out"] == 5
    assert rows["known-model"]["cache_read"] == "unknown"
    assert rows["unknown-model"]["token_in"] == "unknown"
    assert rows["unknown-model"]["token_out"] == "unknown"

    with SessionLocal() as db:
        stored = db.scalar(select(UsageSummary).where(UsageSummary.model == "unknown-model"))
        assert stored is not None and stored.token_in is None
