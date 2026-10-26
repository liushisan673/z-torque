from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import TenantModel


class Conversation(TenantModel):
    __tablename__ = "zr_conversation"
    owner_user_id: Mapped[UUID] = mapped_column(ForeignKey("zr_user.id"), nullable=False, index=True)
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("zr_agent.id"), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    # 两列在迁移里是 timestamptz，模型同步声明 timezone=True
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)