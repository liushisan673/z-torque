"""Add single-instance lease and fencing columns to zr_task (Phase 0 §2.5).

单机语义：lease 只防同进程并发与崩溃残留，不做多副本抢占。
- `lease_owner`：当前执行持有者（runtime 实例 / execute_simulated）。
- `lease_expires_at`：租约到期时间；到期只作为可观测信号，不触发跨实例抢占。
- `lease_heartbeat_at`：最近一次心跳/认领时间。
- `attempt_epoch`：认领时递增的 fencing token；终态写入前校验，拒绝旧持有者写终态。

`tenant_id` 仍保留但值固定，不新增跨租户逻辑。全部列可空或带默认值，
对存量数据是纯加法。

Revision ID: d9e4f7a2b6c1
Revises: f2c3d4e5a6b7
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d9e4f7a2b6c1"
down_revision: str | None = "f2c3d4e5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "zr_task"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("lease_owner", sa.String(128), nullable=True))
    op.add_column(_TABLE, sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("lease_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("attempt_epoch", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column(_TABLE, "attempt_epoch")
    op.drop_column(_TABLE, "lease_heartbeat_at")
    op.drop_column(_TABLE, "lease_expires_at")
    op.drop_column(_TABLE, "lease_owner")
