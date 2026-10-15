from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, TypeDecorator
from sqlalchemy.dialects.sqlite import CHAR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.ids import new_uuid7, utc_now


class GUID(TypeDecorator[UUID]):
    impl = CHAR(36)
    cache_ok = True

    def process_bind_param(self, value: UUID | None, _dialect) -> str | None:
        return str(value) if value else None

    def process_result_value(self, value: Any, _dialect) -> UUID | None:
        return UUID(value) if value else None


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class UUIDModel(Base, TimestampMixin):
    __abstract__ = True
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid7)


class TenantModel(UUIDModel):
    __abstract__ = True
    tenant_id: Mapped[UUID] = mapped_column(GUID(), nullable=False, index=True)
