"""add retry policy to tool versions

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
"""
import sqlalchemy as sa

from alembic import op

revision = "a2b3c4d5e6f7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("zr_tool_version", sa.Column("timeout_seconds", sa.Float(), nullable=False, server_default="30"))
    op.add_column("zr_tool_version", sa.Column("max_retries", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("zr_tool_version", sa.Column("retry_backoff_seconds", sa.Float(), nullable=False, server_default="0.5"))


def downgrade() -> None:
    op.drop_column("zr_tool_version", "retry_backoff_seconds")
    op.drop_column("zr_tool_version", "max_retries")
    op.drop_column("zr_tool_version", "timeout_seconds")
