"""P2 加固回归：能力端点脱敏、独立事务审计、MCP 异常映射、确认单可见性。"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.session import SessionLocal
from app.kernel.audit.service import record_audit, record_audit_detached
from app.kernel.skill_gateway import SkillGateway
from app.main import app
from app.models import AuditLog, Confirmation, Role, Tenant, User, user_role
from app.security.passwords import hash_password


def _make_tenant() -> Tenant:
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"hardening-{uuid4()}")
        db.add(tenant)
        db.flush()
        return tenant


def _make_user(tenant_id, *, admin: bool = False) -> dict:
    username = f"h-{uuid4()}"
    with SessionLocal.begin() as db:
        user = User(tenant_id=tenant_id, username=username, password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        if admin:
            role = Role(tenant_id=tenant_id, name="admin")
            db.add(role)
            db.flush()
            db.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
        return {"id": user.id, "tenant_id": tenant_id, "username": username, "password": "pass"}


async def _login(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post("/api/z-torque/auth/login", json={"username": username, "password": "pass"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


@pytest.mark.asyncio
async def test_capabilities_anonymous_and_non_admin_are_redacted():
    """P2-15：匿名/普通用户不得拿到部署路径与出站白名单（且匿名不得 500）。"""
    tenant = _make_tenant()
    member = _make_user(tenant.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.get("/api/z-torque/capabilities")
        assert anonymous.status_code == 200, anonymous.text
        headers = await _login(client, member["username"])
        signed_in = await client.get("/api/z-torque/capabilities", headers=headers)
        assert signed_in.status_code == 200, signed_in.text

    for payload in (anonymous.json()["data"], signed_in.json()["data"]):
        assert "directory" not in payload["md_skill"]
        assert "storage_dir" not in payload["files"]
        assert "allowed_targets" not in payload["connectors"]


@pytest.mark.asyncio
async def test_capabilities_admin_sees_deployment_details():
    tenant = _make_tenant()
    admin = _make_user(tenant.id, admin=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = await _login(client, admin["username"])
        response = await client.get("/api/z-torque/capabilities", headers=headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["md_skill"]["directory"]
    assert data["files"]["storage_dir"]
    assert "allowed_targets" in data["connectors"]


def test_record_audit_detached_survives_outer_rollback():
    """P2-12：调用方事务回滚时，拒绝审计必须已经落库（独立事务）。"""
    tenant = _make_tenant()

    with SessionLocal() as db:
        record_audit(db, tenant_id=tenant.id, action="ROLLED_BACK_EVENT", resource_type="TEST", resource_id=None, actor_user_id=None, summary="should vanish")
        db.rollback()

    assert record_audit_detached(
        tenant_id=tenant.id,
        action="AUTHORIZATION_DENIED",
        resource_type="TEST",
        resource_id=None,
        actor_user_id=None,
        summary="detached survives rollback",
    ) is not None

    with SessionLocal() as db:
        actions = set(db.scalars(select(AuditLog.action).where(AuditLog.tenant_id == tenant.id)).all())
    assert "AUTHORIZATION_DENIED" in actions
    assert "ROLLED_BACK_EVENT" not in actions


class _BrokenConnector:
    def __init__(self, *, fail_on: str) -> None:
        self.fail_on = fail_on

    async def negotiate(self) -> None:
        if self.fail_on == "negotiate":
            raise RuntimeError("transport exploded")

    async def call_tool(self, name, arguments):
        if self.fail_on == "call":
            raise RuntimeError("tool exploded")
        return {"ok": True}

    async def close(self) -> None:
        return None


def test_mcp_negotiation_failure_is_mapped_not_raised():
    gateway = SkillGateway(http_connector_factory=lambda *a, **kw: _BrokenConnector(fail_on="negotiate"))
    result = asyncio.run(
        gateway._execute_mcp({"endpoint": "https://mcp.example.com", "transport": "http", "tool_name": "t"}, {}, 10)
    )
    assert result.status == "FAILED"
    assert result.error_code == "AI_MCP_NEGOTIATION_FAILED"


def test_mcp_tool_call_failure_is_mapped_not_raised():
    gateway = SkillGateway(http_connector_factory=lambda *a, **kw: _BrokenConnector(fail_on="call"))
    result = asyncio.run(
        gateway._execute_mcp({"endpoint": "https://mcp.example.com", "transport": "http", "tool_name": "t"}, {}, 10)
    )
    assert result.status == "FAILED"
    assert result.error_code == "AI_MCP_TOOL_CALL_FAILED"


@pytest.mark.asyncio
async def test_confirmation_visibility_is_scoped_to_requester_or_admin():
    """P1-11：普通成员看不到他人确认单（工具入参），管理员可见且能识别发起人。"""
    tenant = _make_tenant()
    owner = _make_user(tenant.id)
    peer = _make_user(tenant.id)
    admin = _make_user(tenant.id, admin=True)

    with SessionLocal.begin() as db:
        confirmation = Confirmation(
            tenant_id=tenant.id,
            requested_by_user_id=owner["id"],
            reason="需要人工确认",
            request_snapshot={"arguments": {"secret": "x"}},
            status="PENDING",
        )
        db.add(confirmation)
        db.flush()
        confirmation_id = confirmation.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_headers = await _login(client, owner["username"])
        peer_headers = await _login(client, peer["username"])
        admin_headers = await _login(client, admin["username"])

        owner_list = await client.get("/api/z-torque/confirmations", headers=owner_headers)
        peer_list = await client.get("/api/z-torque/confirmations", headers=peer_headers)
        admin_list = await client.get("/api/z-torque/confirmations", headers=admin_headers)
        peer_detail = await client.get(f"/api/z-torque/confirmations/{confirmation_id}", headers=peer_headers)

    assert [item["id"] for item in owner_list.json()["data"]] == [str(confirmation_id)]
    assert str(confirmation_id) not in [item["id"] for item in peer_list.json()["data"]]
    assert peer_detail.status_code == 404

    admin_items = [item for item in admin_list.json()["data"] if item["id"] == str(confirmation_id)]
    assert admin_items and admin_items[0]["requested_by_username"] == owner["username"]
