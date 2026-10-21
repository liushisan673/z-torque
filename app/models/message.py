from typing import Any
from uuid import UUID

from sqlalchemy import Column, ForeignKey, JSON, String, Table, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import TenantModel

message_file = Table(
    "zr_message_file",
    TenantModel.metadata,
    Column("message_id", ForeignKey("zr_message.id", ondelete="CASCADE"), primary_key=True),
    Column("file_id", ForeignKey("zr_file.id", ondelete="RESTRICT"), primary_key=True),
    Column("tenant_id", ForeignKey("zr_tenant.id"), nullable=False),
)


class Message(TenantModel):
    __tablename__ = "zr_message"
    # 原 (id, conversation_id) 唯一约束是 id 主键的冗余组合，数据库层从未创建，
    # 也无复合外键引用它（task 的 trigger/result_message_id 均单列引用 id），故移除声明。
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("zr_conversation.id", ondelete="RESTRICT"), nullable=False, index=True)
    sender_type: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_parts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="COMPLETED")
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("zr_task.id", ondelete="RESTRICT"), nullable=True, unique=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)