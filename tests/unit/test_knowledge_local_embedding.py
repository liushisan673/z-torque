from types import SimpleNamespace

import pytest

from app.builtin_plugins.knowledge import embeddings


class _FakeVector(list):
    def tolist(self):
        return list(self)


class _FakeModel:
    def embed(self, texts, batch_size=32):
        assert batch_size > 0
        return [_FakeVector([0.1, 0.2, 0.3]) for _ in texts]


def test_local_embedding_uses_cpu_model_without_http(monkeypatch, tmp_path):
    (tmp_path / "onnx").mkdir()
    (tmp_path / "onnx" / "model.onnx").write_bytes(b"model")
    settings = SimpleNamespace(
        embedding_enabled=True,
        embedding_provider="local",
        embedding_base_url="",
        embedding_model="BAAI/bge-small-zh-v1.5",
        embedding_batch_size=8,
        embedding_timeout_seconds=2,
        embedding_api_key=None,
    )
    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)
    monkeypatch.setattr(embeddings, "local_model_cache_dir", lambda: tmp_path)
    client = embeddings.EmbeddingClient()
    monkeypatch.setattr(client, "_load_local_model", lambda: _FakeModel())

    assert client.configured is True
    assert client.model_key == "local:BAAI/bge-small-zh-v1.5"
    assert client.embed(["知识库语义检索", "本地 Agent 平台"]) == [
        [0.1, 0.2, 0.3],
        [0.1, 0.2, 0.3],
    ]
    client.close()


def test_local_embedding_fails_closed_when_assets_are_missing(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        embedding_enabled=True,
        embedding_provider="local",
        embedding_base_url="",
        embedding_model="BAAI/bge-small-zh-v1.5",
        embedding_batch_size=8,
        embedding_timeout_seconds=2,
        embedding_api_key=None,
    )
    monkeypatch.setattr(embeddings, "get_settings", lambda: settings)
    monkeypatch.setattr(embeddings, "local_model_cache_dir", lambda: tmp_path)
    client = embeddings.EmbeddingClient()

    assert client.configured is False
    with pytest.raises(embeddings.EmbeddingError):
        client.embed(["测试"])
    client.close()
