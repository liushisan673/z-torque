"""agent is_default flag

Revision ID: f2c3d4e5a6b7
Revises: a8c2e5d7b1f3
Create Date: 2026-11-20

M12-4：为 zr_agent 增加 is_default 标记（用户指定的默认智能体，区别于平台默认父 Agent）。
存量数据全部 false；同租户最多一行 true（部分唯一索引，写法同 uq_zr_agent_default_parent）。

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2c3d4e5a6b7"
down_revision: str | None = "a8c2e5d7b1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "zr_agent"
_INDEX = "uq_zr_agent_default"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index(
        _INDEX,
        _TABLE,
        ["tenant_id", "is_default"],
        unique=True,
        sqlite_where=sa.text("is_default = 1"),
        postgresql_where=sa.text("is_default"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "is_default")
