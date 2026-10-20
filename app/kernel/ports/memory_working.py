from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID


class WorkingMemoryStore(ABC):
    """memory.working Port 契约：请求级工作记忆（契约 §3 信息族）。

    以 (tenant, user, conversation) 为键的请求级状态缓存；社区版内置进程内
    LRU 缓存（容量 settings.memory_working_cache_size），企业版可替换为
    Redis 等共享后端。
    """

    @abstractmethod
    def put(self, tenant_id: UUID, user_id: UUID, conversation_id: UUID, value: dict[str, Any]) -> None:
        """写入/覆盖该会话的工作记忆快照（浅拷贝存储）。"""

    @abstractmethod
    def get(self, tenant_id: UUID, user_id: UUID, conversation_id: UUID) -> dict[str, Any] | None:
        """读取工作记忆快照；不存在返回 None（返回副本，调用方修改不影响缓存）。"""

    @abstractmethod
    def delete(self, tenant_id: UUID, user_id: UUID, conversation_id: UUID) -> None:
        """删除工作记忆快照；键不存在时静默（幂等）。"""
