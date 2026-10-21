from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Table, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import TenantModel, UUIDModel


class Tenant(UUIDModel):
    __tablename__ = "zr_tenant"
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)


user_role = Table(
    "zr_user_role",
    TenantModel.metadata,
    Column("user_id", ForeignKey("zr_user.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", ForeignKey("zr_role.id", ondelete="CASCADE"), primary_key=True),
)


role_permission = Table(
    "zr_role_permission",
    TenantModel.metadata,
    Column("role_id", ForeignKey("zr_role.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", ForeignKey("zr_permission.id", ondelete="CASCADE"), primary_key=True),
)


class User(TenantModel):
    __tablename__ = "zr_user"

    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    failed_login_count: Mapped[int] = mapped_column(default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Role(TenantModel):
    __tablename__ = "zr_role"
    # 角色名在租户内唯一（与 admin_iam 的按租户查重一致）；全局唯一会让第二个租户建同名角色时
    # 绕过应用层校验后在 DB 层抛 IntegrityError
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_zr_role_tenant_name"),)

    name: Mapped[str] = mapped_column(String(128), nullable=False)


class Permission(TenantModel):
    __tablename__ = "zr_permission"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_zr_permission_tenant_name"),)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
