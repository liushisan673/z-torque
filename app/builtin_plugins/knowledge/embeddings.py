"""本地 ONNX 与兼容 API Embedding 适配器；绝不生成伪向量。"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import httpx

from app.config import get_settings

DEFAULT_LOCAL_MODEL = "BAAI/bge-small-zh-v1.5"


class EmbeddingError(RuntimeError):
    pass


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def local_model_cache_dir() -> Path:
    """源码运行使用仓库缓存；PyInstaller onedir 使用随包分发的只读资源目录。"""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if _is_frozen() and frozen_root:
        return Path(frozen_root) / "models" / "embedding-cache"
    return Path(__file__).resolve().parents[3] / "models" / "embedding-cache"


def local_model_assets_available() -> bool:
    """只在冻结安装包中要求模型预置；源码模式允许首次使用时由 FastEmbed 下载。"""
    cache = local_model_cache_dir()
    return cache.is_dir() and any(cache.rglob("*.onnx"))


class EmbeddingClient:
    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self.settings = get_settings()
        self.client = client or httpx.Client(trust_env=False)
        self._owns_client = client is None
        self.last_error: str | None = None
        self._local_model = None

    @property
    def provider(self) -> str:
        return str(self.settings.embedding_provider or "local").strip().lower()

    @property
    def configured(self) -> bool:
        if not self.settings.embedding_enabled:
            return False
        if self.provider == "local":
            # 开发环境首次运行自动下载模型；冻结安装包必须确实包含模型资源，禁止运行时联网。
            return not _is_frozen() or local_model_assets_available()
        return bool(self.settings.embedding_base_url.strip() and self.settings.embedding_model.strip())

    @property
    def model(self) -> str:
        return str(self.settings.embedding_model or DEFAULT_LOCAL_MODEL).strip()

    @property
    def model_key(self) -> str:
        """向量空间标识；模型/Provider 变化时隔离旧向量，禁止跨模型混检。"""
        return f"{self.provider}:{self.model}"

    def _load_local_model(self):
        if self._local_model is None:
            cache = local_model_cache_dir()
            if _is_frozen() and not local_model_assets_available():
                self.last_error = "ModelAssetsMissing"
                raise EmbeddingError("bundled local embedding model assets are missing")
            # 源码开发模式允许首次调用时下载到仓库缓存；桌面安装包严格离线加载。
            cache.mkdir(parents=True, exist_ok=True)
            if _is_frozen():
                os.environ["HF_HUB_OFFLINE"] = "1"
                os.environ["TRANSFORMERS_OFFLINE"] = "1"
            try:
                from fastembed import TextEmbedding

                self._local_model = TextEmbedding(model_name=self.model, cache_dir=str(cache))
            except Exception as exc:
                self.last_error = type(exc).__name__
                raise EmbeddingError("local embedding model could not be loaded") from exc
        return self._local_model

    @staticmethod
    def _normalize_vectors(vectors, expected_count: int) -> list[list[float]]:
        if not isinstance(vectors, (list, tuple)) or len(vectors) != expected_count:
            raise EmbeddingError("embedding response item count is invalid")
        result: list[list[float]] = []
        for vector in vectors:
            if hasattr(vector, "tolist"):
                vector = vector.tolist()
            if not isinstance(vector, (list, tuple)) or not vector:
                raise EmbeddingError("embedding vector is empty")
            values = [float(value) for value in vector]
            if any(not math.isfinite(value) for value in values):
                raise EmbeddingError("embedding vector contains invalid values")
            result.append(values)
        if len({len(value) for value in result}) != 1:
            raise EmbeddingError("embedding response has inconsistent dimensions")
        return result

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.configured:
            raise EmbeddingError("embedding is disabled, not configured, or bundled model assets are missing")
        if not texts:
            return []
        batch_size = max(1, min(int(self.settings.embedding_batch_size), 128))
        if self.provider == "local":
            try:
                vectors = list(self._load_local_model().embed(texts, batch_size=batch_size))
                result = self._normalize_vectors(vectors, len(texts))
                self.last_error = None
                return result
            except EmbeddingError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                raise EmbeddingError("local embedding inference failed") from exc

        output: list[list[float] | None] = [None] * len(texts)
        for offset in range(0, len(texts), batch_size):
            batch = texts[offset:offset + batch_size]
            try:
                provider = self.provider
                if provider == "ollama":
                    response = self.client.post(
                        self.settings.embedding_base_url.rstrip("/") + "/api/embed",
                        json={"model": self.model, "input": batch},
                        timeout=self.settings.embedding_timeout_seconds,
                    )
                    response.raise_for_status()
                    vectors = response.json().get("embeddings")
                elif provider in {"openai", "openai-compatible"}:
                    base = self.settings.embedding_base_url.rstrip("/")
                    url = base if base.endswith("/embeddings") else base + "/embeddings"
                    headers = {}
                    if self.settings.embedding_api_key is not None:
                        headers["Authorization"] = "Bearer " + self.settings.embedding_api_key.get_secret_value()
                    response = self.client.post(
                        url, headers=headers, json={"model": self.model, "input": batch},
                        timeout=self.settings.embedding_timeout_seconds,
                    )
                    response.raise_for_status()
                    data = response.json().get("data")
                    vectors = [
                        item.get("embedding")
                        for item in sorted(data, key=lambda item: item.get("index", 0))
                    ] if isinstance(data, list) else None
                else:
                    raise EmbeddingError("unsupported embedding provider")
                normalized = self._normalize_vectors(vectors, len(batch))
                for index, vector in enumerate(normalized):
                    output[offset + index] = vector
            except EmbeddingError:
                raise
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                self.last_error = type(exc).__name__
                raise EmbeddingError("embedding provider request failed") from exc
        vectors_out = [value for value in output if value is not None]
        if len(vectors_out) != len(texts) or len({len(value) for value in vectors_out}) != 1:
            raise EmbeddingError("embedding response is incomplete or has inconsistent dimensions")
        self.last_error = None
        return vectors_out

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
