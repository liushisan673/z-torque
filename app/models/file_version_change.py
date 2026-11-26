"""会话内同名文件的版本变更（文件对比审查的数据源）。

与 PatchArtifact 不同：这里记录的是智能体/用户再次输出同名文件后，
前后两版可读文本（extracted_text）的差异；审计只记元数据，正文只落本表。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import GUID, TenantModel

FILE_REVIEW_PENDING = "PENDING"
FILE_REVIEW_KEPT = "KEPT"
FILE_REVIEW_CANCELLED = "CANCELLED"
FILE_REVIEW_UNDONE = "UNDONE"

FILE_CHANGE_CREATE = "CREATE"
FILE_CHANGE_UPDATE = "UPDATE"
FILE_CHANGE_ROLLBACK = "ROLLBACK"


class FileVersionChange(TenantModel):
    __tablename__ = "zr_file_version_change"

    owner_user_id: Mapped[UUID] = mapped_column(ForeignKey("zr_user.id"), nullable=False, index=True)
    conversation_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("zr_conversation.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    task_id: Mapped[UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    previous_file_id: Mapped[UUID | None] = mapped_column(GUID(), nullable=True)
    current_file_id: Mapped[UUID] = mapped_column(GUID(), nullable=False)
    message_id: Mapped[UUID | None] = mapped_column(GUID(), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=FILE_CHANGE_UPDATE, server_default=FILE_CHANGE_UPDATE)
    fmt: Mapped[str | None] = mapped_column(String(16), nullable=True)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_text: Mapped[str] = mapped_column(Text, nullable=False)
    after_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    additions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deletions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    review_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FILE_REVIEW_PENDING, server_default=FILE_REVIEW_PENDING,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
