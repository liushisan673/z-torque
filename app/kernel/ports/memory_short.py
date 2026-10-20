from abc import ABC, abstractmethod
from uuid import UUID

from sqlalchemy.orm import Session

from app.kernel.model_gateway import ModelGateway
from app.models import Conversation, ShortTermMemory


class ShortTermMemoryProvider(ABC):
    """memory.short Port 契约：会话级短记忆（契约 §3 信息族）。

    摘要生成依赖 model.provider（经参数传入，与调用方一致）；社区版内置
    DB 存储（ShortTermMemory 表 + SUPERSEDED 摘要链），企业版可替换实现。
    """

    @abstractmethod
    async def summarize(self, db: Session, *, conversation: Conversation, user_id: UUID, trace_id: str, model_gateway: ModelGateway) -> ShortTermMemory:
        """按上次摘要位置增量生成会话摘要并落库（旧摘要置 SUPERSEDED）。"""

    @abstractmethod
    def active_memory(self, db: Session, *, tenant_id: UUID, user_id: UUID, agent_id: UUID | None, conversation_id: UUID) -> ShortTermMemory | None:
        """取会话当前 ACTIVE 摘要记录；不存在返回 None。"""
