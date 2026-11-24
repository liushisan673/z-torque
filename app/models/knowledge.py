"""本地知识库文档与带坐标 chunk（Phase 0 §2.1 / §八）。

FTS5 虚表不建 ORM 模型（由迁移 raw SQL 创建 + 插件 raw SQL 查询），避免
alembic autogenerate 对虚表产生漂移；tenant_id 保留但单机固定值。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import GUID, TenantModel


class KnowledgeDoc(TenantModel):
    __tablename__ = "zr_knowledge_doc"

    file_id: Mapped[UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE", index=True)
    truncated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KnowledgeChunk(TenantModel):
    __tablename__ = "zr_knowledge_chunk"
    __table_args__ = (Index("ix_zr_knowledge_chunk_doc_seq", "doc_id", "seq", unique=True),)

    doc_id: Mapped[UUID] = mapped_column(ForeignKey("zr_knowledge_doc.id", ondelete="CASCADE"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sheet: Mapped[str | None] = mapped_column(String(128), nullable=True)
    row_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    col_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
