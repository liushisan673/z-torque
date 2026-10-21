"""Tenant-scoped role/permission uniqueness and user profile columns.

修复两处模型与迁移不一致：
1. zr_user 缺 display_name / last_login_at（旧库仅靠运行时 SQLite ALTER 打补丁，非 SQLite 方言登录即报未定义列）；
2. zr_role / zr_permission 的 name 为全局唯一，与 admin_iam 按租户查重的口径冲突，
   第二个租户创建同名角色会绕过接口校验后在 DB 层抛 IntegrityError。
"""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2f5c8d1a4b73"
down_revision: str | None = "1a2b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLite 无法直接删除匿名的表级 UNIQUE 约束：batch 模式反射时按该约定命名，才能 drop_constraint
_NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _inspector():
    return sa.inspect(op.get_bind())


def _drop_stale_batch_tables(*tables: str) -> None:
    """SQLite batch 模式中途失败会残留 _alembic_tmp_<table>，导致重跑时"table already exists"。"""
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    existing = {row[0] for row in bind.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table in tables:
        temporary = f"_alembic_tmp_{table}"
        if temporary in existing:
            bind.exec_driver_sql(f'DROP TABLE "{temporary}"')


def _column_names(table: str) -> set[str]:
    return {column["name"] for column in _inspector().get_columns(table)}


def _unique_constraints(table: str) -> dict[tuple[str, ...], str | None]:
    return {tuple(uc["column_names"]): uc.get("name") for uc in _inspector().get_unique_constraints(table)}


def _drop_legacy_name_uniqueness(table: str) -> None:
    """把 name 单列全局唯一改为 (tenant_id, name) 租户内唯一。"""
    unique = _unique_constraints(table)
    if ("tenant_id", "name") in unique:
        return  # 已迁移
    legacy_name: str | None = None
    if ("name",) in unique:
        # SQLite 的匿名约束反射为 None，batch 模式会按 naming_convention 命名
        legacy_name = unique[("name",)] or f"uq_{table}_name"
    with op.batch_alter_table(table, naming_convention=_NAMING_CONVENTION) as batch_op:
        if legacy_name is not None:
            batch_op.drop_constraint(legacy_name, type_="unique")
        batch_op.create_unique_constraint(f"uq_{table}_tenant_name", ["tenant_id", "name"])


def _restore_global_name_uniqueness(table: str) -> None:
    unique = _unique_constraints(table)
    with op.batch_alter_table(table, naming_convention=_NAMING_CONVENTION) as batch_op:
        if ("tenant_id", "name") in unique:
            batch_op.drop_constraint(unique[("tenant_id", "name")] or f"uq_{table}_tenant_name", type_="unique")
        if ("name",) not in unique:
            batch_op.create_unique_constraint(f"uq_{table}_name", ["name"])


def upgrade() -> None:
    _drop_stale_batch_tables("zr_user", "zr_role", "zr_permission")
    columns = _column_names("zr_user")
    with op.batch_alter_table("zr_user") as batch_op:
        if "display_name" not in columns:
            batch_op.add_column(sa.Column("display_name", sa.String(128), nullable=True))
        if "last_login_at" not in columns:
            batch_op.add_column(sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))

    _drop_legacy_name_uniqueness("zr_role")
    _drop_legacy_name_uniqueness("zr_permission")


def downgrade() -> None:
    _drop_stale_batch_tables("zr_user", "zr_role", "zr_permission")
    _restore_global_name_uniqueness("zr_permission")
    _restore_global_name_uniqueness("zr_role")

    columns = _column_names("zr_user")
    with op.batch_alter_table("zr_user") as batch_op:
        if "last_login_at" in columns:
            batch_op.drop_column("last_login_at")
        if "display_name" in columns:
            batch_op.drop_column("display_name")
