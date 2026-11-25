from uuid import UUID

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import GUID, TenantModel


class KnowledgeBase(TenantModel):
    """用户拥有的知识资源集合；文档源文件仍由 File 统一管理。"""

    __tablename__ = "zr_knowledge_base"
    __table_args__ = (
        UniqueConstraint("tenant_id", "owner_user_id", "name", name="uq_zr_knowledge_base_owner_name"),
        Index("ix_zr_knowledge_base_owner_status", "owner_user_id", "status"),
    )

    owner_user_id: Mapped[UUID] = mapped_column(ForeignKey("zr_user.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")


class KnowledgeBaseDocument(TenantModel):
    """知识库与用户文件的多对多关联，不复制原始文件或索引内容。"""

    __tablename__ = "zr_knowledge_base_document"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "file_id", name="uq_zr_kb_document"),
        Index("ix_zr_kb_document_file", "file_id"),
    )

    knowledge_base_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("zr_knowledge_base.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("zr_file.id", ondelete="RESTRICT"), nullable=False
    )
