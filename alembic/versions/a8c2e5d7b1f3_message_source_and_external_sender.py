"""message source and external sender

Revision ID: a8c2e5d7b1f3
Revises: e7b3c1d9a2f4
Create Date: 2026-11-20

为 zr_message 增加来源渠道与外部发送者字段：外部系统（通用 Webhook）回调消息
不再以 owner 用户身份落库，改用 sender_type=EXTERNAL 并携带 source/external_sender。
两列均可空，存量数据不回填（读取时 NULL 视为 console 来源）。

"""
from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a8c2e5d7b1f3"
down_revision: str | None = "e7b3c1d9a2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "zr_message"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("source", sa.String(length=32), nullable=True))
    op.add_column(_TABLE, sa.Column("external_sender", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "external_sender")
    op.drop_column(_TABLE, "source")
