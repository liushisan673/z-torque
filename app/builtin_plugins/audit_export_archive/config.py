"""审计导出/归档插件配置（v1.3 §5.4）。

配置唯一来源为全局 Settings（env / .env），插件只经 `PluginSdk.settings` 读取，
不带任何隐式默认值膨胀：缺配置就按声明默认值跑，并在装配期完成校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 对象存储键前缀：RED（导出）与 COLD（归档）物理隔离，删除导出不影响冷副本
EXPORT_PREFIX = "audit-export"
ARCHIVE_PREFIX = "audit-archive"

REDACTION_STANDARD = "standard"
REDACTION_STRICT = "strict"

# 递归防护：这些前缀的动作是"导出/归档自身"产生的，默认不出现在导出物里
SELF_ACTION_PREFIX = "AUDIT_"

# CSV 列顺序固定（导出件是外部契约，列顺序变更需版本化）
CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "tenant_id",
    "actor_user_id",
    "action",
    "category",
    "resource_type",
    "resource_id",
    "summary",
    "detail",
    "trace_id",
    "created_at",
)


@dataclass(frozen=True)
class AuditExportConfig:
    max_rows: int
    chunk_rows: int
    max_concurrency: int
    retry_limit: int
    hot_retention_days: int
    archive_encryption_enabled: bool
    archive_schedule_hours: float
    # 与 Settings / config_schema 的声明默认值保持一致（仅 jsonl；csv 需显式放开）
    export_formats: tuple[str, ...] = ("jsonl",)
    exclude_own_actions: bool = True
    redaction_profile: str = REDACTION_STANDARD
    redaction_key: str = ""
    archive_secret_ref: str = ""
    # 轮换过渡期：上一把密钥只读（解密历史对象），新写入恒用 archive_secret_ref
    archive_previous_secret_ref: str = ""
    export_prefix: str = EXPORT_PREFIX
    archive_prefix: str = ARCHIVE_PREFIX
    # 单对象大小上限：分片按 chunk_rows 切分后远小于该值，仅作存储侧兜底
    max_object_bytes: int = 512 * 1024 * 1024
    # ---- 多副本租约（v1.3 §5.9）----
    # 任务执行权靠 DB 行级租约仲裁：TTL 到期即视为持有者失联。
    # 续租间隔必须显著小于 TTL，否则抖动一次就会被判定为失联。
    lease_ttl_seconds: float = 60.0
    lease_renew_interval_seconds: float = 20.0
    # 实例标识：留空则由 host:pid:rand 自动生成（多副本务必保持唯一）
    instance_id: str = ""
    # ---- 热窗口维护（过期清理）----
    # 清理周期秒数：<=0 关闭定时清理（冷启动/测试场景），生产默认每小时一次。
    # 没有这个定时器时 `purge_expired` 只能被测试直接调用，热区对象会无限期堆积。
    purge_interval_seconds: float = 3600.0
    # 单轮清理最多处理多少条到期任务（避免一次大扫拖住维护线程）
    purge_batch_limit: int = 100

    @classmethod
    def from_settings(cls, settings: Any) -> "AuditExportConfig":
        raw_prefix = str(getattr(settings, "audit_archive_prefix", ARCHIVE_PREFIX) or "").strip().strip("/")
        raw_formats = getattr(settings, "audit_export_formats", None) or ["jsonl"]
        if isinstance(raw_formats, str):
            raw_formats = [item.strip() for item in raw_formats.split(",") if item.strip()]
        return cls(
            max_rows=int(getattr(settings, "audit_export_max_rows", 100_000)),
            chunk_rows=int(getattr(settings, "audit_export_chunk_rows", 1_000)),
            max_concurrency=int(getattr(settings, "audit_export_max_concurrency", 1)),
            retry_limit=int(getattr(settings, "audit_export_retry_limit", 3)),
            hot_retention_days=int(getattr(settings, "audit_export_hot_retention_days", 90)),
            archive_encryption_enabled=bool(getattr(settings, "audit_archive_encryption_enabled", False)),
            archive_schedule_hours=float(getattr(settings, "audit_archive_schedule_hours", 24.0)),
            export_formats=tuple(str(item).lower() for item in raw_formats),
            exclude_own_actions=bool(getattr(settings, "audit_export_exclude_own_actions", True)),
            redaction_profile=str(getattr(settings, "audit_export_redaction_profile", REDACTION_STANDARD) or REDACTION_STANDARD).lower(),
            redaction_key=str(getattr(settings, "audit_export_redaction_key", "") or ""),
            archive_secret_ref=str(getattr(settings, "audit_archive_secret_ref", "") or ""),
            archive_previous_secret_ref=str(getattr(settings, "audit_archive_previous_secret_ref", "") or ""),
            export_prefix=EXPORT_PREFIX,
            archive_prefix=raw_prefix or ARCHIVE_PREFIX,
            lease_ttl_seconds=float(getattr(settings, "audit_export_lease_ttl_seconds", 60.0)),
            lease_renew_interval_seconds=float(getattr(settings, "audit_export_lease_renew_interval_seconds", 20.0)),
            instance_id=str(getattr(settings, "audit_export_instance_id", "") or "").strip(),
            # 与 schema 声明保持一致：此前该 Settings 项从未被消费，改 env 实际不生效
            max_object_bytes=int(getattr(settings, "audit_export_max_object_bytes", 512 * 1024 * 1024)),
            purge_interval_seconds=float(getattr(settings, "audit_export_purge_interval_seconds", 3600.0)),
            purge_batch_limit=int(getattr(settings, "audit_export_purge_batch_limit", 100)),
        )

    def validate(self) -> None:
        if self.max_rows < 1:
            raise ValueError("audit_export_max_rows must be positive")
        if not 1 <= self.chunk_rows <= self.max_rows:
            raise ValueError("audit_export_chunk_rows must be within 1..audit_export_max_rows")
        if self.max_concurrency < 1:
            raise ValueError("audit_export_max_concurrency must be >= 1")
        if self.retry_limit < 0:
            raise ValueError("audit_export_retry_limit must be >= 0")
        if self.hot_retention_days < 1:
            raise ValueError("audit_export_hot_retention_days must be >= 1")
        if self.archive_schedule_hours <= 0:
            raise ValueError("audit_archive_schedule_hours must be positive")
        if not self.archive_prefix:
            raise ValueError("audit_archive_prefix must not be empty")
        if not self.export_formats:
            raise ValueError("audit_export_formats must not be empty")
        unknown = [item for item in self.export_formats if item not in ("jsonl", "csv")]
        if unknown:
            raise ValueError(f"audit_export_formats contains unsupported formats: {unknown}")
        if self.redaction_profile not in (REDACTION_STANDARD, REDACTION_STRICT):
            raise ValueError("audit_export_redaction_profile must be 'standard' or 'strict'")
        # strict 的 HMAC 密钥缺失 → 装配期拒绝，不得运行期降级为明文主体 id
        if self.redaction_profile == REDACTION_STRICT and not self.redaction_key:
            raise ValueError("audit_export_redaction_key is required when audit_export_redaction_profile=strict")
        if self.archive_encryption_enabled and not self.archive_secret_ref:
            raise ValueError("audit_archive_secret_ref is required when audit_archive_encryption_enabled=true")
        # 轮换过渡期约束：上一把密钥只能在加密开启时存在，且不能与当前密钥同引用
        if self.archive_previous_secret_ref and not self.archive_encryption_enabled:
            raise ValueError("audit_archive_previous_secret_ref requires audit_archive_encryption_enabled=true")
        if self.archive_previous_secret_ref and self.archive_previous_secret_ref == self.archive_secret_ref:
            raise ValueError("audit_archive_previous_secret_ref must differ from audit_archive_secret_ref")
        # 租约：TTL 至少 5 秒（低于此值等同"必然过期"），续租间隔必须落在 (0, ttl) 内
        if self.lease_ttl_seconds < 5:
            raise ValueError("audit_export_lease_ttl_seconds must be >= 5")
        if not 0 < self.lease_renew_interval_seconds < self.lease_ttl_seconds:
            raise ValueError("audit_export_lease_renew_interval_seconds must be within (0, audit_export_lease_ttl_seconds)")
        if self.max_object_bytes < 1:
            raise ValueError("audit_export_max_object_bytes must be positive")
        if self.purge_interval_seconds < 0:
            raise ValueError("audit_export_purge_interval_seconds must be >= 0")
        if self.purge_batch_limit < 1:
            raise ValueError("audit_export_purge_batch_limit must be >= 1")
