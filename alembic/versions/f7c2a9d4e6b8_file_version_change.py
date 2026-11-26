"""Add file version changes for conversation file review.

Revision ID: f7c2a9d4e6b8
Revises: a6d2e8f4b7c1
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f7c2a9d4e6b8"
down_revision: str | None = "a6d2e8f4b7c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "zr_file_version_change"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("owner_user_id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("previous_file_id", sa.String(36), nullable=True),
        sa.Column("current_file_id", sa.String(36), nullable=False),
        sa.Column("message_id", sa.String(36), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="UPDATE"),
        sa.Column("fmt", sa.String(16), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("risk", sa.Text(), nullable=True),
        sa.Column("before_text", sa.Text(), nullable=False),
        sa.Column("after_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("additions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deletions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("review_state", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["zr_user.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["zr_conversation.id"], ondelete="CASCADE"),
    )
    for column in ("tenant_id", "owner_user_id", "conversation_id", "task_id", "name"):
        op.create_index(f"ix_{_TABLE}_{column}", _TABLE, [column])


def downgrade() -> None:
    for column in ("tenant_id", "owner_user_id", "conversation_id", "task_id", "name"):
        op.drop_index(f"ix_{_TABLE}_{column}", table_name=_TABLE)
    op.drop_table(_TABLE)
