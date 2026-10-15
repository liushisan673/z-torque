import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["code"] == 0
    assert response.json()["data"]["database"] is True


@pytest.mark.asyncio
async def test_auth_requires_credentials():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/z-torque/auth/me")
    assert response.status_code == 401
    assert response.json()["code"] == "AI_AUTH_FAILED"
