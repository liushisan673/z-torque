from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import GUID, TenantModel, UUIDModel
from app.kernel.task.states import TaskStatus


class Task(TenantModel):
    __tablename__ = "zr_task"
    __table_args__ = (
        UniqueConstraint("tenant_id", "owner_user_id", "idempotency_key"),
        Index("uq_task_parent_child_sequence", "tenant_id", "parent_task_id", "child_sequence", unique=True),
        Index("uq_task_delegation", "tenant_id", "delegation_id", unique=True),
    )

    owner_user_id: Mapped[UUID] = mapped_column(ForeignKey("zr_user.id"), nullable=False, index=True)
    # 以下 UUID 列统一使用项目 GUID（36 位带连字符）：原生 Uuid 在 SQLite 存 32 位无连字符，
    # 与主键格式不一致会导致裸 SQL 关联失配
    parent_task_id: Mapped[UUID | None] = mapped_column(GUID(), ForeignKey("zr_task.id"), nullable=True)
    root_task_id: Mapped[UUID] = mapped_column(GUID(), nullable=False, index=True)
    delegation_depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    child_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delegation_id: Mapped[UUID | None] = mapped_column(nullable=True)
    depends_on_task_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    conversation_id: Mapped[UUID | None] = mapped_column(GUID(), nullable=True)
    # Canonical Stage 3 field. message_id remains a legacy alias at the API boundary.
    trigger_message_id: Mapped[UUID | None] = mapped_column(GUID(), nullable=True)
    message_id: Mapped[UUID | None] = mapped_column(GUID(), nullable=True)
    result_message_id: Mapped[UUID | None] = mapped_column(GUID(), nullable=True)
    agent_id: Mapped[UUID] = mapped_column(GUID(), nullable=False)
    agent_version_id: Mapped[UUID] = mapped_column(GUID(), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(String(32), default=TaskStatus.PENDING, nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_summary: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_event_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class TaskEvent(UUIDModel):
    __tablename__ = "zr_task_event"
    __table_args__ = (UniqueConstraint("task_id", "seq"),)

    tenant_id: Mapped[UUID] = mapped_column(GUID(), nullable=False, index=True)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("zr_task.id", ondelete="CASCADE"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class TaskCheckpoint(TenantModel):
    __tablename__ = "zr_task_checkpoint"
    task_id: Mapped[UUID] = mapped_column(ForeignKey("zr_task.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="WAITING_APPROVAL")
    execution_context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    completed_steps: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    pending_steps: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    pending_call: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    confirmation_id: Mapped[UUID | None] = mapped_column(ForeignKey("zr_confirmation.id", ondelete="SET NULL"), nullable=True, index=True)
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Outbox(UUIDModel):
    __tablename__ = "zr_outbox"

    tenant_id: Mapped[UUID] = mapped_column(GUID(), nullable=False, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(GUID(), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
