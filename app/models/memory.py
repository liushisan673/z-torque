from uuid import UUID

from sqlalchemy import ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import TenantModel


class ShortTermMemory(TenantModel):
    __tablename__ = "zr_memory_short_term"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("zr_user.id"), nullable=False, index=True)
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("zr_agent.id"), nullable=False, index=True)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("zr_conversation.id", ondelete="CASCADE"), nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    source_start: Mapped[int] = mapped_column(Integer, nullable=False)
    source_end: Mapped[int] = mapped_column(Integer, nullable=False)
    source_message_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")