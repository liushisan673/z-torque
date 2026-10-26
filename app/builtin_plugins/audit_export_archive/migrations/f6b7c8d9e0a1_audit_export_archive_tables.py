"""Add audit export / archive tables (v1.3 §5.6).

三张表是 `audit_export_archive` 插件唯一的持久化面（类型 II「自有表」）：
`zr_audit_export_job` / `zr_audit_export_shard` / `zr_audit_archive_batch`。
GUID 统一 CHAR(36)（与项目 GUID 一致），JSON 列在 SQLite/PG 均映射为 sa.JSON；
索引与模型 `__table_args__` 一一对应，保证 compare_metadata 无漂移。

Revision ID: f6b7c8d9e0a1
Revises: e5c1f7a92b40
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f6b7c8d9e0a1"
down_revision: str | None = "e5c1f7a92b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUID = sa.CHAR(length=36)
_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "zr_audit_export_job",
        sa.Column("id", _GUID, primary_key=True),
        sa.Column("tenant_id", _GUID, nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.Column("created_by", _GUID, sa.ForeignKey("zr_user.id"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("query_json", sa.JSON(), nullable=False),
        sa.Column("format", sa.String(16), nullable=False, server_default="jsonl"),
        sa.Column("rows_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shards_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("size_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("object_prefix", sa.String(512), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", _TS, nullable=True),
        sa.Column("finished_at", _TS, nullable=True),
        sa.Column("expires_at", _TS, nullable=True),
    )
    op.create_index("ix_zr_audit_export_job_tenant_id", "zr_audit_export_job", ["tenant_id"])
    op.create_index("ix_zr_audit_export_job_created_by", "zr_audit_export_job", ["created_by"])
    op.create_index("ix_zr_audit_export_job_status", "zr_audit_export_job", ["status"])
    op.create_index("ix_zr_audit_export_job_expires_at", "zr_audit_export_job", ["expires_at"])
    op.create_index("ix_zr_audit_export_job_scope", "zr_audit_export_job", ["tenant_id", "status", "created_at"])

    op.create_table(
        "zr_audit_export_shard",
        sa.Column("id", _GUID, primary_key=True),
        sa.Column("tenant_id", _GUID, nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.Column("job_id", _GUID, sa.ForeignKey("zr_audit_export_job.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.UniqueConstraint("job_id", "seq", name="uq_zr_audit_export_shard_seq"),
    )
    op.create_index("ix_zr_audit_export_shard_tenant_id", "zr_audit_export_shard", ["tenant_id"])
    op.create_index("ix_zr_audit_export_shard_job_id", "zr_audit_export_shard", ["job_id"])

    op.create_table(
        "zr_audit_archive_batch",
        sa.Column("id", _GUID, primary_key=True),
        sa.Column("tenant_id", _GUID, nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.Column("created_by", _GUID, sa.ForeignKey("zr_user.id"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("trigger_source", sa.String(16), nullable=False, server_default="MANUAL"),
        sa.Column("window_start", _TS, nullable=False),
        sa.Column("window_end", _TS, nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("objects", sa.JSON(), nullable=False),
        sa.Column("sha256_total", sa.String(64), nullable=True),
        sa.Column("size_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("object_prefix", sa.String(512), nullable=True),
        sa.Column("encryption", sa.String(32), nullable=False, server_default="none"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", _TS, nullable=True),
        sa.Column("finished_at", _TS, nullable=True),
    )
    op.create_index("ix_zr_audit_archive_batch_tenant_id", "zr_audit_archive_batch", ["tenant_id"])
    op.create_index("ix_zr_audit_archive_batch_created_by", "zr_audit_archive_batch", ["created_by"])
    op.create_index("ix_zr_audit_archive_batch_status", "zr_audit_archive_batch", ["status"])
    op.create_index("ix_zr_audit_archive_batch_scope", "zr_audit_archive_batch", ["tenant_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_zr_audit_archive_batch_scope", table_name="zr_audit_archive_batch")
    op.drop_index("ix_zr_audit_archive_batch_status", table_name="zr_audit_archive_batch")
    op.drop_index("ix_zr_audit_archive_batch_created_by", table_name="zr_audit_archive_batch")
    op.drop_index("ix_zr_audit_archive_batch_tenant_id", table_name="zr_audit_archive_batch")
    op.drop_table("zr_audit_archive_batch")

    op.drop_index("ix_zr_audit_export_shard_job_id", table_name="zr_audit_export_shard")
    op.drop_index("ix_zr_audit_export_shard_tenant_id", table_name="zr_audit_export_shard")
    op.drop_table("zr_audit_export_shard")

    op.drop_index("ix_zr_audit_export_job_scope", table_name="zr_audit_export_job")
    op.drop_index("ix_zr_audit_export_job_expires_at", table_name="zr_audit_export_job")
    op.drop_index("ix_zr_audit_export_job_status", table_name="zr_audit_export_job")
    op.drop_index("ix_zr_audit_export_job_created_by", table_name="zr_audit_export_job")
    op.drop_index("ix_zr_audit_export_job_tenant_id", table_name="zr_audit_export_job")
    op.drop_table("zr_audit_export_job")
