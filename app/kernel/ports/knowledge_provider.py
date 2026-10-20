from abc import ABC, abstractmethod
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class KnowledgeChunk:
    source: str
    content: str
    score: float | None = None
    metadata: dict | None = None


class KnowledgeProvider(ABC):
    """knowledge.provider Port 契约：知识库检索（契约 §3 信息族）。

    社区版无内置实现（capabilities 显式 UNAVAILABLE）；知识库能力按契约 §4
    以插件形式接入，不得反向新增内核 Port。
    """

    @abstractmethod
    def search(self, *, tenant_id: UUID, query: str, top_k: int = 5, filters: dict | None = None) -> list[KnowledgeChunk]:
        """检索知识片段，返回按相关性降序的 top_k 结果。"""
