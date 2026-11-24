import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.context_governance import estimate_tokens, resolve_chars_per_token


def test_model_specific_chars_per_token_and_fallback():
    assert resolve_chars_per_token("deepseek-chat", 2.0) == 2.1
    assert resolve_chars_per_token("gpt-4o", 2.0) == 2.2
    assert resolve_chars_per_token("unknown-model", 2.0) == 2.0
    assert estimate_tokens("a" * 21, 2.1) == 10


@pytest.mark.asyncio
async def test_capabilities_reports_lightweight_estimator():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/z-torque/capabilities")
    assert response.status_code == 200
    context = response.json()["data"]["context"]
    assert context["estimator"] == "model_chars_per_token"
    assert context["tokenizer_dependency"] is False
