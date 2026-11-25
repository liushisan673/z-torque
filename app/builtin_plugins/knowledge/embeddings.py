"""Real Embedding adapters. No synthetic/hash/random vector fallback."""
from __future__ import annotations

import math

import httpx

from app.config import get_settings


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient:
    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self.settings = get_settings()
        self.client = client or httpx.Client(trust_env=False)
        self._owns_client = client is None
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.settings.embedding_enabled and self.settings.embedding_base_url.strip() and self.settings.embedding_model.strip())

    @property
    def model(self) -> str:
        return self.settings.embedding_model.strip()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.configured:
            raise EmbeddingError("embedding is not enabled or configured")
        if not texts:
            return []
        batch_size = max(1, min(int(self.settings.embedding_batch_size), 128))
        output: list[list[float] | None] = [None] * len(texts)
        for offset in range(0, len(texts), batch_size):
            batch = texts[offset:offset + batch_size]
            try:
                provider = self.settings.embedding_provider.lower()
                if provider == "ollama":
                    response = self.client.post(self.settings.embedding_base_url.rstrip("/") + "/api/embed", json={"model": self.model, "input": batch}, timeout=self.settings.embedding_timeout_seconds)
                    response.raise_for_status()
                    vectors = response.json().get("embeddings")
                elif provider in {"openai", "openai-compatible"}:
                    base = self.settings.embedding_base_url.rstrip("/")
                    url = base if base.endswith("/embeddings") else base + "/embeddings"
                    headers = {}
                    if self.settings.embedding_api_key is not None:
                        headers["Authorization"] = "Bearer " + self.settings.embedding_api_key.get_secret_value()
                    response = self.client.post(url, headers=headers, json={"model": self.model, "input": batch}, timeout=self.settings.embedding_timeout_seconds)
                    response.raise_for_status()
                    data = response.json().get("data")
                    vectors = [item.get("embedding") for item in sorted(data, key=lambda item: item.get("index", 0))] if isinstance(data, list) else None
                else:
                    raise EmbeddingError("unsupported embedding provider")
                if not isinstance(vectors, list) or len(vectors) != len(batch):
                    raise EmbeddingError("embedding response item count is invalid")
                for index, vector in enumerate(vectors):
                    if not isinstance(vector, list) or not vector:
                        raise EmbeddingError("embedding vector is empty")
                    values = [float(value) for value in vector]
                    if any(not math.isfinite(value) for value in values):
                        raise EmbeddingError("embedding vector contains invalid values")
                    output[offset + index] = values
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
