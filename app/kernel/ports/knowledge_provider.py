"""knowledge.provider Port 契约：本地检索 + 结构化索引（Phase 0 §2.1）。

21 Port 冻结：不新增 Port，只在既有 `knowledge.provider` 上做窄扩展
（index/delete/reindex/mode）。社区版内置实现放 `app/builtin_plugins/knowledge/`。
坐标块与检索结果共用同一份不可变契约，保证「引用坐标能回查 chunk 表」。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class ParsedBlock:
    """结构化解析块：至少带一种可回查坐标。"""

    text: str
    page_no: int | None = None
    sheet: str | None = None
    row_no: int | None = None
    col_no: int | None = None
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True)
class ParsedFile:
    text: str
    blocks: tuple[ParsedBlock, ...]
    truncated: bool = False


@dataclass(frozen=True)
class KnowledgeChunk:
    source: str
    content: str
    score: float | None = None
    metadata: dict | None = None


class KnowledgeProvider(ABC):
    """knowledge.provider Port 契约（契约 §3 信息族）。

    社区版内置 SQLite FTS5 实现；企业版可经 PortRegistry 替换。新增方法均为窄扩展，
    默认显式 `AI_CAPABILITY_UNAVAILABLE`，不静默退化。
    """

    @abstractmethod
    def search(self, *, tenant_id: UUID, query: str, top_k: int = 5, filters: dict | None = None, db: Any = None) -> list[KnowledgeChunk]:
        """检索知识片段，返回按相关性降序的 top_k 结果。

        窄扩展 `db`：本地 SQLite 实现需要调用方 Session；不传时显式不可用。
        """

    def index_file(self, db: Any, *, tenant_id: UUID, file_id: UUID, path: str, sha256: str, parsed: ParsedFile, size_bytes: int) -> dict:
        raise NotImplementedError("AI_CAPABILITY_UNAVAILABLE: index_file")

    def delete_file(self, db: Any, *, tenant_id: UUID, file_id: UUID) -> None:
        raise NotImplementedError("AI_CAPABILITY_UNAVAILABLE: delete_file")

    def reindex(self, db: Any, *, tenant_id: UUID) -> dict:
        raise NotImplementedError("AI_CAPABILITY_UNAVAILABLE: reindex")

    def mode(self) -> str:
        return "UNAVAILABLE"
