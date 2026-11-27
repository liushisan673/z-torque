"""P1a-3: add usage summary table (NULL = unknown token)

Revision ID: e6f1a7b3c9d2
Revises: d7e9f2a4b6c8
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op

revision: str = "e6f1a7b3c9d2"
down_revision: str | None = "d7e9f2a4b6c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    # Offline DDL is rendered against a clean target database; MockConnection
    # cannot be inspected, so emit the table creation deterministically.
    if context.is_offline_mode():
        return False
    return sa.inspect(op.get_bind()).has_table(name)


def _has_index(table: str, name: str) -> bool:
    if context.is_offline_mode():
        return False
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        return bool(connection.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).scalar())
    return any(item.get("name") == name for item in sa.inspect(connection).get_indexes(table))


def _create_index(name: str, table: str, columns, unique: bool = False) -> None:
    if not _has_index(table, name):
        op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    if not _has_table("zr_usage_summary"):
        op.create_table(
            "zr_usage_summary",
            sa.Column("id", sa.String(36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("tenant_id", sa.String(36), nullable=False),
            sa.Column("agent_id", sa.String(36), nullable=True),
            sa.Column("model", sa.String(128), nullable=False),
            sa.Column("window", sa.String(32), nullable=False),
            sa.Column("token_in", sa.Integer(), nullable=True),
            sa.Column("token_out", sa.Integer(), nullable=True),
            sa.Column("cache_read", sa.Integer(), nullable=True),
            sa.Column("cache_write", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["agent_id"], ["zr_agent.id"], name="fk_zr_usage_summary_agent_zr_agent", ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index("ix_zr_usage_summary_tenant_id", "zr_usage_summary", ["tenant_id"])
    _create_index("ix_zr_usage_summary_agent_id", "zr_usage_summary", ["agent_id"])
    _create_index("ix_zr_usage_summary_window", "zr_usage_summary", ["window"])
    _create_index("ix_zr_usage_summary_scope", "zr_usage_summary", ["tenant_id", "agent_id", "model", "window"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_zr_usage_summary_scope", table_name="zr_usage_summary")
    op.drop_index("ix_zr_usage_summary_window", table_name="zr_usage_summary")
    op.drop_index("ix_zr_usage_summary_agent_id", table_name="zr_usage_summary")
    op.drop_index("ix_zr_usage_summary_tenant_id", table_name="zr_usage_summary")
    op.drop_table("zr_usage_summary")
