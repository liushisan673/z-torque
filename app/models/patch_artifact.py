"""补丁 diff 独立存储（路线图 v2.0 §2.3 / §八）。

审计只记 artifact_id + hash + path + size（metadata-only）；before/after 正文
只落本表并带 TTL，单机固定 tenant_id，不设计跨租户隔离。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import GUID, TenantModel


class PatchArtifact(TenantModel):
    __tablename__ = "zr_patch_artifact"

    task_id: Mapped[UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    before_text: Mapped[str] = mapped_column(Text, nullable=False)
    after_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
