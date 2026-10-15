"""Persist task checkpoints before entering human approval wait state."""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d8e5f6a7b890"
down_revision: str | None = "c7f4a2b91d30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "zr_task_checkpoint",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("zr_task.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("execution_context", sa.JSON(), nullable=False),
        sa.Column("completed_steps", sa.JSON(), nullable=False),
        sa.Column("pending_steps", sa.JSON(), nullable=False),
        sa.Column("pending_call", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("confirmation_id", sa.String(36), sa.ForeignKey("zr_confirmation.id", ondelete="SET NULL")),
        sa.Column("resumed_at", sa.DateTime()),
    )
    op.create_index("ix_zr_task_checkpoint_task_id", "zr_task_checkpoint", ["task_id"], unique=True)
    op.create_index("ix_zr_task_checkpoint_confirmation_id", "zr_task_checkpoint", ["confirmation_id"])


def downgrade() -> None:
    op.drop_index("ix_zr_task_checkpoint_confirmation_id", table_name="zr_task_checkpoint")
    op.drop_index("ix_zr_task_checkpoint_task_id", table_name="zr_task_checkpoint")
    op.drop_table("zr_task_checkpoint")
