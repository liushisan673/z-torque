from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class VectorRecord:
    id: str
    vector: list[float]
    metadata: dict | None = None


@dataclass(frozen=True)
class VectorMatch:
    id: str
    score: float
    metadata: dict | None = None


class VectorStore(ABC):
    """vector.store Port 契约：向量检索（契约 §3 信息族）。

    社区版无内置实现（capabilities 显式 UNAVAILABLE，调用抛
    AI_CAPABILITY_UNAVAILABLE，不崩、不静默）；企业版可接入向量数据库实现。
    """

    @abstractmethod
    def upsert(self, *, namespace: str, records: list[VectorRecord]) -> None:
        """按命名空间写入/覆盖向量记录。"""

    @abstractmethod
    def search(self, *, namespace: str, query: list[float], top_k: int = 5) -> list[VectorMatch]:
        """按命名空间近邻检索，返回按相似度降序的 top_k 结果。"""

    @abstractmethod
    def delete(self, *, namespace: str, ids: list[str]) -> None:
        """删除指定向量记录；id 不存在时静默（幂等）。"""
