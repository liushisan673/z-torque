"""add confirmation policy to skill and tool versions

Revision ID: f1a2b3c4d5e6
Revises: d8e5f6a7b890
"""
import sqlalchemy as sa

from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "d8e5f6a7b890"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("zr_tool_version", sa.Column("requires_confirmation", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("zr_skill_version", sa.Column("requires_confirmation", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("zr_skill_version", "requires_confirmation")
    op.drop_column("zr_tool_version", "requires_confirmation")
