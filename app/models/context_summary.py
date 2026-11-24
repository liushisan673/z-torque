"""上下文压缩摘要版本化（路线图 v2.0 §2.4 / §八）。

单机语义：tenant_id 保留但固定值；无跨租户逻辑。
每次成功压缩写入一行（compaction_seq 递增），失败不写 summary 只落 audit。
"""

from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import TenantModel


class ContextSummary(TenantModel):
    __tablename__ = "zr_context_summary"

    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("zr_conversation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    compaction_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_history_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
