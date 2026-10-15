from typing import Any
from uuid import UUID

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import TenantModel


class ModelProvider(TenantModel):
    __tablename__ = "zr_model_provider"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ENABLED", index=True)
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    secret_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("zr_user.id"), nullable=False)


class ModelResource(TenantModel):
    __tablename__ = "zr_model"

    provider_id: Mapped[UUID] = mapped_column(ForeignKey("zr_model_provider.id", ondelete="RESTRICT"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_identifier: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ENABLED", index=True)
    supports_streaming: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supports_function_calling: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ModelEndpoint(TenantModel):
    __tablename__ = "zr_model_endpoint"

    provider_id: Mapped[UUID] = mapped_column(ForeignKey("zr_model_provider.id", ondelete="RESTRICT"), nullable=False, index=True)
    model_id: Mapped[UUID] = mapped_column(ForeignKey("zr_model.id", ondelete="RESTRICT"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoint_url: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ENABLED", index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
