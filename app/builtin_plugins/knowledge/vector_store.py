"""本地持久化向量存储：精确余弦相似度检索，不依赖外部向量数据库。"""

from __future__ import annotations

import math
import struct
from typing import Any

from sqlalchemy import select

from app.core.errors import ApiError
from app.kernel.ports.vector_store import VectorMatch, VectorRecord, VectorStore
from app.models.knowledge_vector import KnowledgeVector

_MAX_DIMENSION = 8192
_MAX_TOP_K = 50


class LocalVectorStore(VectorStore):
    """适用于单机社区版的精确向量检索。

    向量以 float32 二进制持久化，检索时在当前 namespace 内做精确 cosine 排序。
    它不是 ANN 索引；后续数据规模增长时可替换为本地 HNSW/SQLite 扩展实现。
    """

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _validate_vector(vector: list[float]) -> None:
        if not vector or len(vector) > _MAX_DIMENSION:
            raise ApiError("AI_REQUEST_INVALID", "vector dimension is invalid", 422)
        if any(not math.isfinite(float(value)) for value in vector):
            raise ApiError("AI_REQUEST_INVALID", "vector contains a non-finite value", 422)

    @staticmethod
    def _pack(vector: list[float]) -> bytes:
        return struct.pack("<" + "f" * len(vector), *(float(value) for value in vector))

    @staticmethod
    def _unpack(data: bytes, dimension: int) -> tuple[float, ...]:
        if dimension <= 0 or len(data) != dimension * 4:
            raise ValueError("stored vector payload is corrupt")
        return struct.unpack("<" + "f" * dimension, data)

    def upsert(self, *, namespace: str, records: list[VectorRecord]) -> None:
        if not namespace or len(namespace) > 512:
            raise ApiError("AI_REQUEST_INVALID", "vector namespace is invalid", 422)
        if not records:
            return
        ids: list[str] = []
        prepared: dict[str, tuple[int, bytes, dict | None]] = {}
        for record in records:
            if not record.id or len(record.id) > 64:
                raise ApiError("AI_REQUEST_INVALID", "vector record id is invalid", 422)
            self._validate_vector(record.vector)
            if record.id in prepared:
                raise ApiError("AI_REQUEST_INVALID", "duplicate vector record id in batch", 422)
            ids.append(record.id)
            prepared[record.id] = (
                len(record.vector),
                self._pack(record.vector),
                dict(record.metadata) if record.metadata is not None else None,
            )
        with self._session_factory() as db:
            existing = db.scalars(
                select(KnowledgeVector).where(
                    KnowledgeVector.namespace == namespace,
                    KnowledgeVector.record_id.in_(ids),
                )
            ).all()
            by_id = {row.record_id: row for row in existing}
            for record_id, (dimension, vector_data, metadata) in prepared.items():
                row = by_id.get(record_id)
                if row is None:
                    row = KnowledgeVector(namespace=namespace, record_id=record_id)
                    db.add(row)
                row.dimension = dimension
                row.vector_data = vector_data
                row.record_metadata = metadata
            db.flush()
            db.commit()

    def search(self, *, namespace: str, query: list[float], top_k: int = 5) -> list[VectorMatch]:
        if not namespace or len(namespace) > 512:
            raise ApiError("AI_REQUEST_INVALID", "vector namespace is invalid", 422)
        self._validate_vector(query)
        limit = max(1, min(int(top_k), _MAX_TOP_K))
        query_norm = math.sqrt(math.fsum(float(value) * float(value) for value in query))
        if query_norm == 0:
            return []
        with self._session_factory() as db:
            rows = db.scalars(
                select(KnowledgeVector).where(KnowledgeVector.namespace == namespace)
            ).all()
            matches: list[VectorMatch] = []
            for row in rows:
                if row.dimension != len(query):
                    continue
                try:
                    vector = self._unpack(row.vector_data, row.dimension)
                except (ValueError, struct.error):
                    continue
                norm = math.sqrt(math.fsum(value * value for value in vector))
                if norm == 0:
                    continue
                dot = math.fsum(float(left) * right for left, right in zip(query, vector, strict=True))
                score = dot / (query_norm * norm)
                matches.append(VectorMatch(id=row.record_id, score=score, metadata=row.record_metadata))
        matches.sort(key=lambda item: item.score, reverse=True)
        return matches[:limit]

    def delete(self, *, namespace: str, ids: list[str]) -> None:
        if not ids:
            return
        with self._session_factory() as db:
            rows = db.scalars(
                select(KnowledgeVector).where(
                    KnowledgeVector.namespace == namespace,
                    KnowledgeVector.record_id.in_(ids),
                )
            ).all()
            for row in rows:
                db.delete(row)
            db.flush()
            db.commit()
