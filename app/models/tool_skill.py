from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import GUID, TenantModel, UUIDModel


class Tool(TenantModel):
    __tablename__ = "zr_tool"
    owner_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("zr_user.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="USER")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    official: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 业务标签：OpenAPI 导入时以 spec tags 为初值，后续可由所有者编辑
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # 业务封装 Tool 标识：选择器与模型暴露侧用于区分组合能力
    is_business_tool: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    current_published_version_id: Mapped[UUID | None] = mapped_column(GUID(), nullable=True, index=True)


class ToolVersion(TenantModel):
    __tablename__ = "zr_tool_version"
    __table_args__ = (UniqueConstraint("tool_id", "version_number"),)
    tool_id: Mapped[UUID] = mapped_column(ForeignKey("zr_tool.id", ondelete="RESTRICT"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT", index=True)
    config_schema_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    required_permissions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timeout_seconds: Mapped[float] = mapped_column(default=30.0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retry_backoff_seconds: Mapped[float] = mapped_column(default=0.5, nullable=False)
    snapshot_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Skill(TenantModel):
    __tablename__ = "zr_skill"
    owner_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("zr_user.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    skill_type: Mapped[str] = mapped_column(String(32), nullable=False, default="STANDARD")
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="USER")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    official: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    current_published_version_id: Mapped[UUID | None] = mapped_column(GUID(), nullable=True, index=True)


class SkillVersion(TenantModel):
    __tablename__ = "zr_skill_version"
    __table_args__ = (UniqueConstraint("skill_id", "version_number"),)
    skill_id: Mapped[UUID] = mapped_column(ForeignKey("zr_skill.id", ondelete="RESTRICT"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT", index=True)
    config_schema_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1")
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 调用顺序与参数传递提示：发布时注入模型系统提示，发布后不可修改
    orchestration_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    script_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_permissions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    snapshot_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)


class SkillVersionTool(UUIDModel):
    __tablename__ = "zr_skill_version_tool"
    __table_args__ = (UniqueConstraint("skill_version_id", "tool_version_id"),)
    tenant_id: Mapped[UUID] = mapped_column(GUID(), nullable=False, index=True)
    skill_version_id: Mapped[UUID] = mapped_column(ForeignKey("zr_skill_version.id", ondelete="RESTRICT"), nullable=False)
    tool_version_id: Mapped[UUID] = mapped_column(ForeignKey("zr_tool_version.id", ondelete="RESTRICT"), nullable=False)
    # 绑定顺序：同步执行与模型提示均按此排序，发布时固化进快照
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 绑定角色（prerequisite/normal/followup）与依赖备注，仅软约束
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class SkillExecutionLog(TenantModel):
    __tablename__ = "zr_skill_execution_log"
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("zr_task.id", ondelete="SET NULL"), nullable=True, index=True)
    skill_version_id: Mapped[UUID] = mapped_column(ForeignKey("zr_skill_version.id", ondelete="RESTRICT"), nullable=False, index=True)
    tool_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("zr_tool_version.id", ondelete="RESTRICT"), nullable=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("zr_user.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
