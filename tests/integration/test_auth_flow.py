from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, text

from app.config import get_settings
from app.db.session import SessionLocal
from app.main import app
from app.models import AuthSession, Role, Tenant, User
from app.security.passwords import hash_password


@pytest.fixture(autouse=True)
def admin_user():
    with SessionLocal.begin() as db:
        # This fixture owns the isolated SQLite test database; clear all rows
        # before recreating the authentication baseline.
        db.execute(text("PRAGMA foreign_keys=OFF"))
        db.execute(delete(AuthSession))
        db.execute(delete(User))
        db.execute(delete(Role))
        db.execute(delete(Tenant))
        db.execute(text("PRAGMA foreign_keys=ON"))
        tenant = Tenant(name="test-tenant")
        db.add(tenant)
        db.flush()
        db.add(User(tenant_id=tenant.id, username="admin", password_hash=hash_password("pass")))
    yield None


@pytest.mark.asyncio
async def test_login_me_refresh_logout_revokes_session():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/z-torque/auth/login", json={"username": "admin", "password": "pass"})
        assert login.status_code == 200
        body = login.json()["data"]
        assert body["access_token"]
        assert body["refresh_token"]

        me = await client.get("/api/z-torque/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
        assert me.status_code == 200

        refreshed = await client.post("/api/z-torque/auth/refresh", json={"refresh_token": body["refresh_token"]})
        assert refreshed.status_code == 200
        new_body = refreshed.json()["data"]
        old_refresh = await client.post("/api/z-torque/auth/refresh", json={"refresh_token": body["refresh_token"]})
        assert old_refresh.status_code == 401

        logout = await client.post("/api/z-torque/auth/logout", headers={"Authorization": f"Bearer {new_body['access_token']}"})
        assert logout.status_code == 200
        after_logout = await client.get("/api/z-torque/auth/me", headers={"Authorization": f"Bearer {new_body['access_token']}"})
        assert after_logout.status_code == 401


@pytest.mark.asyncio
async def test_login_lockout_and_success_reset():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(get_settings().login_failure_limit):
            response = await client.post("/api/z-torque/auth/login", json={"username": "admin", "password": "bad"})
            assert response.status_code == 401
        locked = await client.post("/api/z-torque/auth/login", json={"username": "admin", "password": "pass"})
        assert locked.status_code == 401

    with SessionLocal.begin() as db:
        user = db.query(User).filter_by(username="admin").one()
        user.locked_until = datetime.now(UTC) - timedelta(seconds=1)
        user.failed_login_count = 0
