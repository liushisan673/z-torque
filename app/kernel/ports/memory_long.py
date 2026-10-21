from abc import ABC, abstractmethod
from uuid import UUID


class LongTermMemoryStore(ABC):
    """memory.long Port 契约：用户级长期记忆 KV（契约 §3 信息族）。

    以 (tenant, user, key) 存取文本事实；社区版内置进程内 KV（单机、进程重启
    即失，memory_long_enabled 默认关闭）。调用方：context_builder 召回会话摘要、
    runtime._maybe_summarize 写入摘要；企业版可替换为 DB / 向量后端。
    """

    @abstractmethod
    def remember(self, *, tenant_id: UUID, user_id: UUID, key: str, value: str) -> None:
        """写入/覆盖一条长期记忆。"""

    @abstractmethod
    def recall(self, *, tenant_id: UUID, user_id: UUID, key: str) -> str | None:
        """读取长期记忆；不存在返回 None。"""

    @abstractmethod
    def forget(self, *, tenant_id: UUID, user_id: UUID, key: str) -> None:
        """删除长期记忆；键不存在时静默（幂等）。"""
