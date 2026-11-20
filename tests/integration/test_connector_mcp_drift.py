"""MCP 连接器漂移检测与同步。

锁定三条关键行为：
  1. /discover 会把远端 tools/list 与本地登记比对，返回 added/removed/changed，
     但**不写库**（发现 ≠ 授权，也不自动改写已登记工具）；
  2. /tools/sync 只在人工显式确认后写回：说明就地更新，入参 Schema 生成新发布版本，
     旧版本快照保持不变（既有 Skill 绑定不受影响）；
  3. 远端已下线的工具不会被自动删除，只回报原因，避免误删仍被绑定的工具。
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

import app.api.routes.connectors as connectors_module
from app.db.session import SessionLocal
from app.main import app
from app.models import (
    Connector,
    ConnectorTool,
    Permission,
    Role,
    Tenant,
    Tool,
    ToolVersion,
    User,
    role_permission,
    user_role,
)
from app.security.passwords import hash_password
from tests.support.ddl import fk_off, fk_on

CONNECTORS = "/api/z-torque/connectors"


@pytest.fixture(autouse=True)
def baseline():
    with SessionLocal.begin() as db:
        fk_off(db)
        db.execute(delete(user_role))
        db.execute(delete(role_permission))
        db.execute(delete(Permission))
        db.execute(delete(ToolVersion))
        db.execute(delete(ConnectorTool))
        db.execute(delete(Tool))
        db.execute(delete(Connector))
        db.execute(delete(User))
        db.execute(delete(Role))
        db.execute(delete(Tenant))
        fk_on(db)

        tenant = Tenant(name="mcp-drift-tenant")
        db.add(tenant)
        db.flush()

        role = Role(tenant_id=tenant.id, name="admin")
        db.add(role)
        db.flush()
        perms = [Permission(tenant_id=tenant.id, name=n) for n in ("connector.read", "connector.manage")]
        db.add_all(perms)
        db.flush()
        admin = User(tenant_id=tenant.id, username="admin", password_hash=hash_password("pass"))
        db.add(admin)
        db.flush()
        for perm in perms:
            db.execute(role_permission.insert().values(role_id=role.id, permission_id=perm.id))
        db.execute(user_role.insert().values(user_id=admin.id, role_id=role.id))


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client: AsyncClient) -> str:
    resp = await client.post("/api/z-torque/auth/login", json={"username": "admin", "password": "pass"})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


async def _create_mcp_connector(client: AsyncClient, token: str) -> str:
    resp = await client.post(
        CONNECTORS,
        headers=_auth(token),
        json={
            "name": "MCP 服务",
            "connector_type": "MCP",
            "config": {"transport": "http", "endpoint": "http://127.0.0.1:9/mcp"},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


@pytest.mark.asyncio
async def test_discover_reports_drift_and_sync_writes_back(monkeypatch):
    listing = {
        "tools": [
            {
                "name": "search",
                "description": "旧说明",
                "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
            }
        ]
    }

    async def fake_probe(request, config, secret_ref=None, allow_private_network=False):
        return listing

    monkeypatch.setattr(connectors_module, "_probe_mcp", fake_probe)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        cid = await _create_mcp_connector(client, token)

        # 1. 尚未登记：discover 把远端工具全部归入 added，且不落库
        found = await client.post(f"{CONNECTORS}/{cid}/discover", headers=_auth(token))
        assert found.status_code == 200, found.text
        assert [item["name"] for item in found.json()["data"]["drift"]["added"]] == ["search"]
        assert found.json()["data"]["drift"]["changed"] == []

        # 2. 按发现结果登记后，再次发现应无漂移
        reg = await client.post(
            f"{CONNECTORS}/{cid}/tools/register",
            headers=_auth(token),
            json={
                "name": "search",
                "description": "旧说明",
                "tool_type": "MCP",
                "external_name": "search",
                "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
            },
        )
        assert reg.status_code == 200, reg.text
        tool_id = reg.json()["data"]["tool_id"]

        same = await client.post(f"{CONNECTORS}/{cid}/discover", headers=_auth(token))
        assert same.json()["data"]["drift"] == {
            "added": [], "removed": [], "changed": [], "unchanged": 1, "has_drift": False,
        }

        # 3. 远端改了说明与入参：discover 只报告差异，本地登记保持不变
        listing["tools"][0]["description"] = "新说明"
        listing["tools"][0]["inputSchema"] = {
            "type": "object",
            "properties": {"keyword": {"type": "string"}},
            "required": ["keyword"],
        }
        drifted = await client.post(f"{CONNECTORS}/{cid}/discover", headers=_auth(token))
        drift = drifted.json()["data"]["drift"]
        assert drift["has_drift"] is True
        assert drift["changed"][0]["name"] == "search"
        assert drift["changed"][0]["fields"] == ["description", "input_schema"]
        assert drift["changed"][0]["local_description"] == "旧说明"
        assert drift["changed"][0]["remote_description"] == "新说明"

        before = await client.get(f"{CONNECTORS}/{cid}/tools", headers=_auth(token))
        assert before.json()["data"][0]["description"] == "旧说明"
        assert before.json()["data"][0]["published_version_number"] == 1

        # 4. 人工确认后同步：说明就地更新，入参生成 v2
        synced = await client.post(
            f"{CONNECTORS}/{cid}/tools/sync", headers=_auth(token), json={"names": ["search"]}
        )
        assert synced.status_code == 200, synced.text
        assert synced.json()["data"] == {
            "connector_id": cid,
            "updated": [
                {"name": "search", "tool_id": tool_id, "fields": ["description", "input_schema"], "version_number": 2}
            ],
            "skipped": [],
        }

        after = await client.get(f"{CONNECTORS}/{cid}/tools", headers=_auth(token))
        assert after.json()["data"][0]["description"] == "新说明"
        assert after.json()["data"][0]["published_version_number"] == 2

        with SessionLocal() as db:
            versions = db.scalars(
                select(ToolVersion).where(ToolVersion.tool_id == UUID(tool_id)).order_by(ToolVersion.version_number)
            ).all()
            assert [v.version_number for v in versions] == [1, 2]
            # 历史版本快照不可变：既有 Skill 绑定仍 pin v1
            assert versions[0].input_schema["properties"] == {"q": {"type": "string"}}
            assert versions[1].input_schema["properties"] == {"keyword": {"type": "string"}}
            assert db.get(Tool, UUID(tool_id)).description == "新说明"

        # 5. 远端下线：不自动删除本地登记，只回报原因
        listing["tools"] = []
        gone = await client.post(
            f"{CONNECTORS}/{cid}/tools/sync", headers=_auth(token), json={"names": ["search"]}
        )
        assert gone.status_code == 200, gone.text
        assert gone.json()["data"]["updated"] == []
        assert gone.json()["data"]["skipped"] == [
            {"name": "search", "reason": "MCP 服务已不再提供该工具，需人工归档"}
        ]
        still_there = await client.get(f"{CONNECTORS}/{cid}/tools", headers=_auth(token))
        assert len(still_there.json()["data"]) == 1
