"""Add lease columns to zr_audit_export_job (v1.3 §5.7).

多副本执行权仲裁所需的三个列：
- `lease_owner`：持有者 `host:pid:rand`，NULL = 无主；
- `lease_expires_at`：租约到期时间，过期即可被其他实例接管/判失败；
- `attempts`：被抢占次数（server_default="0" 保证存量行可加 NOT NULL 列）。

全部可空或带默认值，因此对存量数据是纯加法，不触发重写。`lease_owner`
建索引：`recover_interrupted` 的门禁条件会按 owner 过滤。

Revision ID: c7a1e4b90d23
Revises: b3d6f9a2c5e8
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7a1e4b90d23"
down_revision: str | None = "b3d6f9a2c5e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "zr_audit_export_job"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("lease_owner", sa.String(128), nullable=True))
    op.add_column(_TABLE, sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_zr_audit_export_job_lease_owner", _TABLE, ["lease_owner"])


def downgrade() -> None:
    op.drop_index("ix_zr_audit_export_job_lease_owner", table_name=_TABLE)
    op.drop_column(_TABLE, "attempts")
    op.drop_column(_TABLE, "lease_expires_at")
    op.drop_column(_TABLE, "lease_owner")
