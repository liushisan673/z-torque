"""Add the minimal Stage 03 audit log."""
from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9c2e7a4b1d60"
down_revision: str | None = "7b1e4d9c2a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "zr_audit_log",
        sa.Column("id", sa.CHAR(36), primary_key=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("actor_user_id", sa.CHAR(36), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.CHAR(36), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["zr_user.id"]),
    )
    op.create_index("ix_zr_audit_log_tenant_id", "zr_audit_log", ["tenant_id"])
    op.create_index("ix_zr_audit_log_actor_user_id", "zr_audit_log", ["actor_user_id"])
    op.create_index("ix_zr_audit_log_action", "zr_audit_log", ["action"])
    op.create_index("ix_zr_audit_log_resource_id", "zr_audit_log", ["resource_id"])


def downgrade() -> None:
    op.drop_index("ix_zr_audit_log_resource_id", table_name="zr_audit_log")
    op.drop_index("ix_zr_audit_log_action", table_name="zr_audit_log")
    op.drop_index("ix_zr_audit_log_actor_user_id", table_name="zr_audit_log")
    op.drop_index("ix_zr_audit_log_tenant_id", table_name="zr_audit_log")
    op.drop_table("zr_audit_log")
