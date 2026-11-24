"""Add scoped long-term memory with immutable keys and version chain (Phase 0 §2.2).

单机语义：tenant_id 保留但值固定；三级作用域 (agent_id, user_id, scope)，
scope ∈ {CONVERSATION, AGENT, USER}；项目级在单机下由 AGENT 承载，**无 project_id 列**。
不可变键 append-only + 版本链；乐观锁 content_hash + version。

Revision ID: f9a7b3c5d182
Revises: e8f4a2c6d317
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f9a7b3c5d182"
down_revision: str | None = "e8f4a2c6d317"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "zr_memory_long"
_VERSION_TABLE = "zr_memory_version"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("zr_agent.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("zr_user.id"), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("key", sa.String(256), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("immutable", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_ref", sa.String(512), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
    )
    for column in ("tenant_id", "agent_id", "user_id", "scope", "status"):
        op.create_index(f"ix_{_TABLE}_{column}", _TABLE, [column])

    op.create_table(
        _VERSION_TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("memory_id", sa.String(36), sa.ForeignKey(f"{_TABLE}.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("superseded_by", sa.String(36), sa.ForeignKey(f"{_TABLE}.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index(f"ix_{_VERSION_TABLE}_tenant_id", _VERSION_TABLE, ["tenant_id"])
    op.create_index(f"ix_{_VERSION_TABLE}_memory_id", _VERSION_TABLE, ["memory_id"])


def downgrade() -> None:
    op.drop_index(f"ix_{_VERSION_TABLE}_memory_id", table_name=_VERSION_TABLE)
    op.drop_index(f"ix_{_VERSION_TABLE}_tenant_id", table_name=_VERSION_TABLE)
    op.drop_table(_VERSION_TABLE)
    for column in ("tenant_id", "agent_id", "user_id", "scope", "status"):
        op.drop_index(f"ix_{_TABLE}_{column}", table_name=_TABLE)
    op.drop_table(_TABLE)
