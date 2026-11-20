"""Conversation last_read_at for unread badges.

存量会话回填为"已读到最新消息"，避免升级后历史消息全部变成未读。
"""
from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7b3c1d9a2f4"
down_revision: str | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("zr_conversation", sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True))
    # 存量数据回填：已读位取最后消息时间（无消息则取 updated_at），升级后不产生历史未读
    op.execute("UPDATE zr_conversation SET last_read_at = COALESCE(last_message_at, updated_at)")


def downgrade() -> None:
    op.drop_column("zr_conversation", "last_read_at")
