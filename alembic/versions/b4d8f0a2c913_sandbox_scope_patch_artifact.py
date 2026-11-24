"""Add patch artifact table for transactional sandbox patches (Phase 0 §2.3).

diff 正文独立存储（带 TTL），审计只记 artifact_id/hash/path/size；
tenant_id 保留但单机固定值，无跨租户设计。

Revision ID: b4d8f0a2c913
Revises: f9a7b3c5d182
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b4d8f0a2c913"
down_revision: str | None = "f9a7b3c5d182"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "zr_patch_artifact"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("before_text", sa.Text(), nullable=False),
        sa.Column("after_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    for column in ("tenant_id", "task_id", "path", "expires_at"):
        op.create_index(f"ix_{_TABLE}_{column}", _TABLE, [column])


def downgrade() -> None:
    for column in ("tenant_id", "task_id", "path", "expires_at"):
        op.drop_index(f"ix_{_TABLE}_{column}", table_name=_TABLE)
    op.drop_table(_TABLE)
