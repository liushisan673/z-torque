"""P1a-4: tool binding selector + depends_on

Revision ID: b1c2d3e4f5a6
Revises: e6f1a7b3c9d2
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "e6f1a7b3c9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    return any(item.get("name") == column for item in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _has_column("zr_skill_version_tool", "selector"):
        op.add_column("zr_skill_version_tool", sa.Column("selector", sa.JSON(), nullable=True))
    if not _has_column("zr_skill_version_tool", "depends_on"):
        op.add_column("zr_skill_version_tool", sa.Column("depends_on", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("zr_skill_version_tool", "depends_on")
    op.drop_column("zr_skill_version_tool", "selector")
