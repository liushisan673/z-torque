"""Add context compaction summary table (Phase 0 §2.4).

单机语义：tenant_id 保留但值固定，不做跨租户逻辑。
每次成功压缩写入一行（compaction_seq 递增），失败不写 summary，只落 audit。
`accepted_history_hash` 用于校验摘要对应的 accepted-history 前缀是否仍然有效。

Revision ID: e8f4a2c6d317
Revises: d9e4f7a2b6c1
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e8f4a2c6d317"
down_revision: str | None = "d9e4f7a2b6c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "zr_context_summary"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("zr_conversation.id", ondelete="CASCADE"), nullable=False),
        sa.Column("compaction_seq", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("accepted_history_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
    )
    op.create_index("ix_zr_context_summary_conversation_id", _TABLE, ["conversation_id"])
    op.create_index("ix_zr_context_summary_tenant_id", _TABLE, ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_zr_context_summary_tenant_id", table_name=_TABLE)
    op.drop_index("ix_zr_context_summary_conversation_id", table_name=_TABLE)
    op.drop_table(_TABLE)
