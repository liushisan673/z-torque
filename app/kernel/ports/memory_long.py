from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.errors import ApiError


class LongTermMemoryStore(ABC):
    """memory.long Port 契约：长期记忆（Phase 0 §2.2 窄扩展，不新增 Port）。

    保留原 3 个无 db 参数的 KV 方法（向后兼容调用方/测试）；新增作用域化方法
    带默认实现：未覆写的替代实现显式上报 AI_CAPABILITY_UNAVAILABLE，
    由 call_optional 落审计降级，不静默退化。

    三级作用域 `(agent_id, user_id, scope)`，scope ∈ {CONVERSATION, AGENT, USER}；
    单机下项目级由 AGENT 承载，无 project_id 列。
    """

    SCOPE_CONVERSATION = "CONVERSATION"
    SCOPE_AGENT = "AGENT"
    SCOPE_USER = "USER"

    @abstractmethod
    def remember(self, *, tenant_id: UUID, user_id: UUID, key: str, value: str) -> None:
        """兼容入口：无作用域 KV 写入（内置实现按 key 前缀推导 scope）。"""

    @abstractmethod
    def recall(self, *, tenant_id: UUID, user_id: UUID, key: str) -> str | None:
        """兼容入口：无作用域 KV 读取。"""

    @abstractmethod
    def forget(self, *, tenant_id: UUID, user_id: UUID, key: str) -> None:
        """兼容入口：无作用域 KV 软删除。"""

    def remember_scoped(
        self, db: Session, *,
        tenant_id: UUID, user_id: UUID, key: str, value: str,
        agent_id: UUID | None = None, scope: str = SCOPE_USER,
        immutable: bool = False, source_ref: str = "unspecified",
        expected_version: int | None = None, expected_hash: str | None = None,
        actor_user_id: UUID | None = None, trace_id: str | None = None,
    ) -> dict[str, Any]:
        """作用域化写入；返回 {memory_id, version, appended}。

        乐观锁：expected_version / expected_hash 与当前 ACTIVE 行不一致时抛
        AI_MEMORY_CONFLICT（不吞掉、不静默覆盖）。immutable 键只能 append 新版本。
        """
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "memory.long scoped write is unavailable", 503)

    def recall_scoped(
        self, db: Session, *,
        tenant_id: UUID, user_id: UUID, key: str,
        agent_id: UUID | None = None, scope: str = SCOPE_USER,
        conversation_id: UUID | None = None,
    ) -> str | None:
        """作用域化读取；CONVERSATION 级默认只对所属会话可见。"""
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "memory.long scoped read is unavailable", 503)

    def recall_bundle(
        self, db: Session, *,
        tenant_id: UUID, user_id: UUID, agent_id: UUID | None,
        conversation_id: UUID | None = None,
    ) -> dict[str, str]:
        """一次性召回三级作用域全部 ACTIVE 记忆，供 context_builder 分流注入。"""
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "memory.long bundle read is unavailable", 503)

    def forget_scoped(
        self, db: Session, *,
        tenant_id: UUID, user_id: UUID, key: str,
        agent_id: UUID | None = None, scope: str = SCOPE_USER,
        actor_user_id: UUID | None = None, trace_id: str | None = None,
    ) -> bool:
        """软删除；返回是否命中。键不存在返回 False（幂等）。"""
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "memory.long scoped delete is unavailable", 503)
