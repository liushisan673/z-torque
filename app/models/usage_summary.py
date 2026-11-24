from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Index, Integer, String

from app.db.base import GUID, TenantModel

NULL_TOKEN_MEANS_UNKNOWN = "usage summary token columns are nullable: NULL means provider did not report the value"


class UsageSummary(TenantModel):
    """按天/自定义窗口的 token 汇总；NULL 代表未知，不写 0。"""

    __tablename__ = "zr_usage_summary"
    __table_args__ = (
        Index("ix_zr_usage_summary_scope", "tenant_id", "agent_id", "model", "window", unique=True),
    )

    # UUID 列统一使用项目 GUID（36 位带连字符）：原生 Uuid 在 SQLite 存 32 位无连字符，
    # 会与全库 CHAR(36) 主键对不上（FK 约束失败）
    agent_id: GUID | None = Column(GUID(), ForeignKey("zr_agent.id", ondelete="CASCADE"), nullable=True, index=True)
    model: str = Column(String(128), nullable=False)
    window: str = Column(String(32), nullable=False, index=True)
    token_in: int | None = Column(Integer, nullable=True)
    token_out: int | None = Column(Integer, nullable=True)
    cache_read: int | None = Column(Integer, nullable=True)
    cache_write: int | None = Column(Integer, nullable=True)
