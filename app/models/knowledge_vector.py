from sqlalchemy import JSON, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUIDModel


class KnowledgeVector(UUIDModel):
    """持久化向量记录；namespace 必须由内核按 tenant/user/knowledge-base 构造。"""

    __tablename__ = "zr_knowledge_vector"
    __table_args__ = (
        UniqueConstraint("namespace", "record_id", name="uq_zr_knowledge_vector_namespace_record"),
    )

    namespace: Mapped[str] = mapped_column(String(512), nullable=False)
    record_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    vector_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    record_metadata: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
