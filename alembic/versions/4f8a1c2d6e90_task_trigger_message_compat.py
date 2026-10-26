"""add canonical task trigger message reference"""
import sqlalchemy as sa

from alembic import op
from app.db.base import GUID

revision = "4f8a1c2d6e90"
down_revision = "3c7e2f1a9d04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("zr_task", sa.Column("trigger_message_id", GUID(), nullable=True))


def downgrade() -> None:
    # This only removes the column introduced by this migration.
    op.drop_column("zr_task", "trigger_message_id")