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


def _settings():
    return SimpleNamespace(
        embedding_enabled=True,
        embedding_provider="local",
        embedding_base_url="",
        embedding_model="BAAI/bge-small-zh-v1.5",
        embedding_batch_size=8,
        embedding_timeout_seconds=2,
        embedding_api_key=None,
    )


def test_source_mode_auto_loads_model_when_cache_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(embeddings, "get_settings", _settings)
    monkeypatch.setattr(embeddings, "local_model_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(embeddings.sys, "frozen", False, raising=False)
    client = embeddings.EmbeddingClient()
    loaded = []

    def load():
        loaded.append(True)
        return _FakeModel()

    monkeypatch.setattr(client, "_load_local_model", load)
    assert client.configured is True
    assert client.embed(["知识库语义检索"]) == [[0.1, 0.2, 0.3]]
    assert loaded == [True]
    client.close()


def test_packaged_mode_requires_bundled_model_assets(monkeypatch, tmp_path):
    monkeypatch.setattr(embeddings, "get_settings", _settings)
    monkeypatch.setattr(embeddings, "local_model_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(embeddings.sys, "frozen", True, raising=False)
    client = embeddings.EmbeddingClient()

    assert client.configured is False
    with pytest.raises(embeddings.EmbeddingError):
        client.embed(["测试"])
    client.close()


def test_local_embedding_uses_cpu_model_without_http(monkeypatch, tmp_path):
    (tmp_path / "onnx").mkdir()
    (tmp_path / "onnx" / "model.onnx").write_bytes(b"model")
    monkeypatch.setattr(embeddings, "get_settings", _settings)
    monkeypatch.setattr(embeddings, "local_model_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(embeddings.sys, "frozen", True, raising=False)
    client = embeddings.EmbeddingClient()
    monkeypatch.setattr(client, "_load_local_model", lambda: _FakeModel())

    assert client.configured is True
    assert client.model_key == "local:BAAI/bge-small-zh-v1.5"
    assert client.embed(["知识库语义检索", "本地 Agent 平台"]) == [
        [0.1, 0.2, 0.3],
        [0.1, 0.2, 0.3],
    ]
    client.close()
