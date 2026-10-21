"""默认父 Agent 唯一索引改为租户级部分索引

原索引仅声明 sqlite_where，PostgreSQL 上会退化为布尔列普通唯一索引，
导致 zr_agent 表只能存在一行 true、一行 false；且未包含 tenant_id，
多租户下无法各自设置默认父 Agent。

Revision ID: d4e9b2c7f1a6
Revises: c1d2e3f4a5b6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e9b2c7f1a6"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "uq_zr_agent_default_parent"
_TABLE = "zr_agent"


def upgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.create_index(
        _INDEX,
        _TABLE,
        ["tenant_id", "is_default_parent"],
        unique=True,
        sqlite_where=sa.text("is_default_parent = 1"),
        postgresql_where=sa.text("is_default_parent"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.create_index(
        _INDEX,
        _TABLE,
        ["is_default_parent"],
        unique=True,
        sqlite_where=sa.text("is_default_parent = 1"),
    )
