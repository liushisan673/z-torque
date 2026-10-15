from typing import Any
from uuid import UUID

from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import TenantModel


class Connector(TenantModel):
    __tablename__ = "zr_connector"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    connector_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT", index=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    secret_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    target_policy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("zr_user.id"), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConnectorTool(TenantModel):
    __tablename__ = "zr_connector_tool"

    connector_id: Mapped[UUID] = mapped_column(ForeignKey("zr_connector.id", ondelete="RESTRICT"), nullable=False, index=True)
    tool_id: Mapped[UUID] = mapped_column(ForeignKey("zr_tool.id", ondelete="RESTRICT"), nullable=False, index=True)
    external_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved: Mapped[bool] = mapped_column(nullable=False, default=False)
