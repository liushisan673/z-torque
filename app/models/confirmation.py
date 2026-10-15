from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import TenantModel


class Confirmation(TenantModel):
    __tablename__ = "zr_confirmation"
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("zr_task.id", ondelete="SET NULL"), nullable=True, index=True)
    skill_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("zr_skill_version.id", ondelete="SET NULL"), nullable=True)
    tool_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("zr_tool_version.id", ondelete="SET NULL"), nullable=True)
    requested_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("zr_user.id"), nullable=False)
    confirmed_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("zr_user.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING", index=True)
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    request_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)
