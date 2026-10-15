from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import TenantModel, UUIDModel


class Agent(TenantModel):
    __tablename__ = "zr_agent"
    owner_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("zr_user.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="USER")
    source_agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("zr_agent.id"), nullable=True)
    source_agent_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    current_published_version_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
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
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    delegation_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allowed_child_agent_versions: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    child_necessity_policy: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    delegation_capability_version: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AgentVersionSkill(UUIDModel):
    __tablename__ = "zr_agent_version_skill"
    __table_args__ = (UniqueConstraint("agent_version_id", "skill_version_id"),)
    tenant_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    agent_version_id: Mapped[UUID] = mapped_column(ForeignKey("zr_agent_version.id", ondelete="RESTRICT"), nullable=False)
    skill_version_id: Mapped[UUID] = mapped_column(ForeignKey("zr_skill_version.id", ondelete="RESTRICT"), nullable=False)