from types import SimpleNamespace

import httpx
import pytest

from app.builtin_plugins.knowledge import embeddings


def test_ollama_embedding_returns_real_vectors(monkeypatch):
    settings = SimpleNamespace(
        embedding_enabled=True, embedding_provider="ollama",
        embedding_base_url="http://localhost:11434", embedding_model="nomic-embed-text",
        embedding_timeout_seconds=5.0, embedding_batch_size=8, embedding_api_key=None,
    )
    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"embeddings": [[1.0, 0.0], [0.0, 1.0]]})
    ))
    adapter = embeddings.EmbeddingClient(client=client)
    assert adapter.embed(["first", "second"]) == [[1.0, 0.0], [0.0, 1.0]]


def test_openai_compatible_embedding_sends_configured_model_and_key(monkeypatch):
    settings = SimpleNamespace(
        embedding_enabled=True, embedding_provider="openai",
        embedding_base_url="https://embedding.example/v1", embedding_model="embed-model",
        embedding_timeout_seconds=5.0, embedding_batch_size=8,
        embedding_api_key=SimpleNamespace(get_secret_value=lambda: "test-secret"),
    )
    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)
    def handler(request):
        assert request.url.path == "/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert b'"model":"embed-model"' in request.content
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.2, 0.8]}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = embeddings.EmbeddingClient(client=client)
    assert adapter.embed(["text"]) == [[0.2, 0.8]]


def test_embedding_disabled_fails_closed_without_fabricating_vectors(monkeypatch):
    settings = SimpleNamespace(
        embedding_enabled=False, embedding_provider="ollama",
        embedding_base_url="http://localhost:11434", embedding_model="nomic-embed-text",
        embedding_timeout_seconds=5.0, embedding_batch_size=8, embedding_api_key=None,
    )
    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)
    adapter = embeddings.EmbeddingClient(client=httpx.Client(transport=httpx.MockTransport(
        lambda request: pytest.fail("disabled embedding must not make a network request")
    )))
    with pytest.raises(embeddings.EmbeddingError):
        adapter.embed(["text"])
