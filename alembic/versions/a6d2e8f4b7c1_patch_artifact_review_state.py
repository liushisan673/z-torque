"""Add review state to patch artifacts for conversation file review.

Revision ID: a6d2e8f4b7c1
Revises: b2c3d4e5f6a7
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a6d2e8f4b7c1"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "zr_patch_artifact"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("base_dir", sa.String(1024), nullable=True))
    op.add_column(_TABLE, sa.Column("review_state", sa.String(16), nullable=False, server_default="PENDING"))
    op.add_column(_TABLE, sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "reviewed_at")
    op.drop_column(_TABLE, "review_state")
    op.drop_column(_TABLE, "base_dir")
