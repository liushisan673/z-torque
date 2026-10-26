"""审计导出与归档的自有表模型（v1.3 §5.6，方案 b：模型随插件目录）。

三张表是插件的**唯一**持久化私域（类型 II「自有表」）：
- `zr_audit_export_job`：单 Job 一行，`status` 仅 PENDING/RUNNING/SUCCEEDED/FAILED；
- `zr_audit_export_shard`：每分片一行，唯一约束 `(job_id, seq)` 保证重试幂等；
- `zr_audit_archive_batch`：归档批次索引，`objects` 存分片对象清单。

方案 b 约定（与契约 §4.3 的措辞补充一致）：
- 表定义与迁移文件都跟随插件目录，**但 DDL 仍只由内核统一 Alembic 链执行**
  （`alembic/versions/` 的 head 链包含本插件的迁移文件）；
- 插件运行时**没有任何 DDL 权限**：只经 `PluginSdk.session()` 做 DML。

审计业务数据**不落这些表**：只能经内核只读缝 `sdk.read_audit` 读取。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.plugin_sdk import TenantModel

STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED)

FORMAT_JSONL = "jsonl"
FORMAT_CSV = "csv"
SUPPORTED_FORMATS = (FORMAT_JSONL, FORMAT_CSV)


class AuditExportJob(TenantModel):
    __tablename__ = "zr_audit_export_job"
    __table_args__ = (Index("ix_zr_audit_export_job_scope", "tenant_id", "status", "created_at"),)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_PENDING, index=True)
    # 创建时定稿的查询条件（tenant_id 不在其中：始终取 SecurityContext）
    query_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    export_format: Mapped[str] = mapped_column("format", String(16), nullable=False, default=FORMAT_JSONL)
    rows_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shards_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    size_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    object_prefix: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 显式 timezone=True：与 TimestampMixin 及迁移脚本保持一致，
    # 否则 PG 上模型(naive)与列(timestamptz)漂移，compare_metadata 会持续报警
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 热窗口过期时间：到期只清理对象与分片，任务行保留用于审计追溯
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    # ---- 多副本租约（v1.3 §5.9）----
    # 持有者标识 `host:pid:rand`；NULL 表示无主（PENDING 或迁移前存量行）。
    # 只有抢到租约的实例才允许执行，租约过期视为持有者失联（可被接管/判失败）。
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 被抢次数：>1 说明发生过租约接管，运维可据此定位"反复重启"的实例
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("zr_user.id"), nullable=True, index=True)


class AuditExportShard(TenantModel):
    __tablename__ = "zr_audit_export_shard"
    __table_args__ = (UniqueConstraint("job_id", "seq", name="uq_zr_audit_export_shard_seq"),)

    job_id: Mapped[UUID] = mapped_column(ForeignKey("zr_audit_export_job.id", ondelete="CASCADE"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditArchiveBatch(TenantModel):
    __tablename__ = "zr_audit_archive_batch"
    __table_args__ = (Index("ix_zr_audit_archive_batch_scope", "tenant_id", "created_at"),)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_PENDING, index=True)
    # MANUAL / SCHEDULE：区分人工触发与定时调度，便于追责
    trigger_source: Mapped[str] = mapped_column(String(16), nullable=False, default="MANUAL")
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 分片对象清单：[{seq, object_key, rows, size_bytes, sha256}]；冷副本不在线可改，索引即事实
    objects: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    sha256_total: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    object_prefix: Mapped[str | None] = mapped_column(String(512), nullable=True)
    encryption: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("zr_user.id"), nullable=True, index=True)
