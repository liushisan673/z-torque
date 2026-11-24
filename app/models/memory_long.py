"""长期记忆落库（路线图 v2.0 §2.2 / §八）。

三级作用域 `(agent_id, user_id, scope)`，scope ∈ {CONVERSATION, AGENT, USER}；
单机下「项目级」由 AGENT 承载，**不建 project_id 列**（全仓无 project 实体）。

不可变键：immutable=true 的键只能 append 新版本或标 SUPERSEDED，禁止原地覆盖；
可变键允许原地更新但必须留版本。乐观锁靠 content_hash + version。
"""

from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import TenantModel


class LongMemory(TenantModel):
    __tablename__ = "zr_memory_long"

    agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("zr_agent.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("zr_user.id"), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(256), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    immutable: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE", index=True)


class LongMemoryVersion(TenantModel):
    __tablename__ = "zr_memory_version"

    memory_id: Mapped[UUID] = mapped_column(ForeignKey("zr_memory_long.id", ondelete="CASCADE"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    superseded_by: Mapped[UUID | None] = mapped_column(ForeignKey("zr_memory_long.id", ondelete="SET NULL"), nullable=True)
