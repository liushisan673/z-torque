from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.main import app
from app.models import Permission, Role, Tenant, User, role_permission, user_role
from app.security.passwords import hash_password


@pytest.fixture(autouse=True)
def iam_baseline():
    with SessionLocal.begin() as db:
        db.execute(delete(user_role))
        db.execute(delete(role_permission))
        db.execute(delete(Permission))
        db.execute(delete(User))
        db.execute(delete(Role))
        db.execute(delete(Tenant))

        tenant = Tenant(name="iam-tenant")
        db.add(tenant)
        db.flush()

        # Roles/permissions
        admin_role = Role(tenant_id=tenant.id, name="admin")
        user_role_item = Role(tenant_id=tenant.id, name="user")
        p_admin_manage = Permission(tenant_id=tenant.id, name="admin.manage")
        p_readonly = Permission(tenant_id=tenant.id, name="readonly")
        db.add_all([admin_role, user_role_item, p_admin_manage, p_readonly])
        db.flush()

        # grant admin.manage to admin role
        db.execute(role_permission.insert().values(role_id=admin_role.id, permission_id=p_admin_manage.id))

        # Users
        admin = User(tenant_id=tenant.id, username="admin", password_hash=hash_password("pass"))
        normal = User(tenant_id=tenant.id, username="normal", password_hash=hash_password("pass"))
        db.add_all([admin, normal])
        db.flush()

        db.execute(user_role.insert().values(user_id=admin.id, role_id=admin_role.id))
        db.execute(user_role.insert().values(user_id=normal.id, role_id=user_role_item.id))

        data = {
            "tenant_id": tenant.id,
            "admin_id": admin.id,
            "normal_id": normal.id,
            "admin_role_id": admin_role.id,
            "user_role_id": user_role_item.id,
            "p_admin_manage_id": p_admin_manage.id,
            "p_readonly_id": p_readonly.id,
        }

    yield data


async def _login(client: AsyncClient, username: str, password: str) -> str:
    resp = await client.post("/api/z-torque/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200
    return resp.json()["data"]["access_token"]


@pytest.mark.asyncio
async def test_auth_me_includes_roles_permissions(iam_baseline):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token_admin = await _login(client, "admin", "pass")
        me_admin = await client.get("/api/z-torque/auth/me", headers={"Authorization": f"Bearer {token_admin}"})
        assert me_admin.status_code == 200
        data_admin = me_admin.json()["data"]
        assert "roles" in data_admin
        assert "permissions" in data_admin
        assert "admin" in data_admin["roles"]
        assert "admin.manage" in data_admin["permissions"] or "user.create" in data_admin["permissions"]

        token_user = await _login(client, "normal", "pass")
        me_user = await client.get("/api/z-torque/auth/me", headers={"Authorization": f"Bearer {token_user}"})
        assert me_user.status_code == 200
        data_user = me_user.json()["data"]
        assert "permissions" in data_user
        assert "user.create" not in data_user["permissions"]


@pytest.mark.asyncio
async def test_admin_iam_forbidden_for_non_admin(iam_baseline):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "normal", "pass")

        r1 = await client.get("/api/z-torque/admin/iam/users", headers={"Authorization": f"Bearer {token}"})
        assert r1.status_code == 403

        r2 = await client.get("/api/z-torque/admin/iam/roles", headers={"Authorization": f"Bearer {token}"})
        assert r2.status_code == 403

        r3 = await client.get("/api/z-torque/admin/iam/permissions", headers={"Authorization": f"Bearer {token}"})
        assert r3.status_code == 403


@pytest.mark.asyncio
async def test_admin_iam_crud_happy_path(iam_baseline):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "admin", "pass")
        headers = {"Authorization": f"Bearer {token}"}

        # list users should include roles/permissions (aggregated from DB)
        lu0 = await client.get("/api/z-torque/admin/iam/users", headers=headers)
        assert lu0.status_code == 200
        items0 = lu0.json()["data"]["items"]
        assert any(u["username"] == "admin" for u in items0)
        admin_item = next(u for u in items0 if u["username"] == "admin")
        assert "roles" in admin_item
        assert "permissions" in admin_item
        assert any(r["name"] == "admin" for r in admin_item["roles"])
        assert any(p["name"] == "admin.manage" for p in admin_item["permissions"])

        # create permission
        cp = await client.post("/api/z-torque/admin/iam/permissions", headers=headers, json={"name": "task.manage"})
        assert cp.status_code == 200

        # create role
        cr = await client.post("/api/z-torque/admin/iam/roles", headers=headers, json={"name": "operator"})
        assert cr.status_code == 200
        role_id = cr.json()["data"]["id"]

        # list permissions to get id for task.manage
        perms = await client.get("/api/z-torque/admin/iam/permissions", headers=headers)
        assert perms.status_code == 200
        perm_items = perms.json()["data"]["items"]
        task_manage = next((p for p in perm_items if p["name"] == "task.manage"), None)
        assert task_manage is not None

        # set role permissions
        srp = await client.post(
            f"/api/z-torque/admin/iam/roles/{role_id}/permissions",
            headers=headers,
            json={"permission_ids": [task_manage["id"]]},
        )
        assert srp.status_code == 200

        # create user
        cu = await client.post(
            "/api/z-torque/admin/iam/users",
            headers=headers,
            json={"username": "alice", "password": "p1", "is_active": True},
        )
        assert cu.status_code == 200
        user_id = cu.json()["data"]["id"]

        # assign roles
        sur = await client.post(
            f"/api/z-torque/admin/iam/users/{user_id}/roles",
            headers=headers,
            json={"role_ids": [role_id]},
        )
        assert sur.status_code == 200

        # get roles
        gur = await client.get(f"/api/z-torque/admin/iam/users/{user_id}/roles", headers=headers)
        assert gur.status_code == 200
        assert role_id in gur.json()["data"]["role_ids"]

        # disable
        pu = await client.patch(f"/api/z-torque/admin/iam/users/{user_id}", headers=headers, json={"is_active": False})
        assert pu.status_code == 200

        # reset password
        rp = await client.post(
            f"/api/z-torque/admin/iam/users/{user_id}/reset-password",
            headers=headers,
            json={"password": "newpass"},
        )
        assert rp.status_code == 200

        # delete (community: disable)
        du = await client.delete(f"/api/z-torque/admin/iam/users/{user_id}", headers=headers)
        assert du.status_code == 200

        lu = await client.get("/api/z-torque/admin/iam/users", headers=headers)
        assert lu.status_code == 200
        items = lu.json()["data"]["items"]
        alice_item = next(u for u in items if u["id"] == user_id)
        assert alice_item["is_active"] is False

        # delete role
        dr = await client.delete(f"/api/z-torque/admin/iam/roles/{role_id}", headers=headers)
        assert dr.status_code == 200
