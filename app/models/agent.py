from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import GUID, TenantModel, UUIDModel


class Agent(TenantModel):
    __tablename__ = "zr_agent"
    # 默认父 Agent 的租户级部分唯一索引（P0-3）：必须带 tenant_id，
    # 且 PG 需要显式 postgresql_where，否则布尔列退化为全表唯一
    # （每个租户只能有一行 true / 一行 false，第二个 Agent 就插不进去）。
    __table_args__ = (
        Index(
            "uq_zr_agent_default_parent",
            "tenant_id",
            "is_default_parent",
            unique=True,
            sqlite_where=text("is_default_parent = 1"),
            postgresql_where=text("is_default_parent"),
        ),
    )
    owner_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("zr_user.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="USER")
    source_agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("zr_agent.id"), nullable=True)
    # UUID 列统一使用项目 GUID（36 位带连字符）：原生 Uuid 在 SQLite 存 32 位无连字符，
    # 与主键格式不一致会导致裸 SQL 关联失配
    source_agent_version_id: Mapped[UUID | None] = mapped_column(GUID(), nullable=True)
    current_published_version_id: Mapped[UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    is_default_parent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)


class AgentVersion(TenantModel):
    __tablename__ = "zr_agent_version"
    __table_args__ = (UniqueConstraint("agent_id", "version_number"),)
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("zr_agent.id"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT", index=True)
    config_schema_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    snapshot_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 迁移里该列是 timestamptz，模型必须声明 timezone=True，
    # 否则 PG 读出来是 aware、模型以为是 naive，跨类型比较会抛 TypeError
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delegation_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allowed_child_agent_versions: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    child_necessity_policy: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    delegation_capability_version: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AgentVersionSkill(UUIDModel):
    __tablename__ = "zr_agent_version_skill"
    __table_args__ = (UniqueConstraint("agent_version_id", "skill_version_id"),)
    tenant_id: Mapped[UUID] = mapped_column(GUID(), nullable=False, index=True)
    agent_version_id: Mapped[UUID] = mapped_column(ForeignKey("zr_agent_version.id", ondelete="RESTRICT"), nullable=False)
    skill_version_id: Mapped[UUID] = mapped_column(ForeignKey("zr_skill_version.id", ondelete="RESTRICT"), nullable=False)