"""审计归档：热窗口 → 冷副本 + 批次索引（v1.3 §5.1 / §5.6）。

与导出的差异（刻意保守）：
- 归档由服务端定时/管理员触发，不接受任意过滤条件，只按"热保留窗口"切窗；
- 冷副本默认明文（`AUDIT_ARCHIVE_ENCRYPTION_ENABLED=false`），开启后 AES-256-GCM
  加密，密文自描述（magic `ZAE1` + 12 字节 nonce），密钥缺失立即失败，绝不降级明文；
- 只生成冷副本 + 索引：**不提供删除审计行 API**，运维清理走独立审批/离线流程。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from datetime import datetime, timedelta
from io import BytesIO
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.builtin_plugins.audit_export_archive.config import AuditExportConfig
from app.builtin_plugins.audit_export_archive.exporter import (
    encode_shard,
    manifest_payload,
    sha256_total,
    to_export_row,
)
from app.builtin_plugins.audit_export_archive.keys import (
    ArchiveKeyRing,
    build_key_ring,
)
from app.builtin_plugins.audit_export_archive.keys import (
    decrypt_archive_payload as decrypt_archive_payload,  # 兼容旧调用点（测试/离线工具）的再导出
)
from app.builtin_plugins.audit_export_archive.keys import (
    derive_key as _derive_key,  # noqa: F401 - 兼容旧调用点（测试直接 import _derive_key）
)
from app.builtin_plugins.audit_export_archive.keys import (
    encrypt_archive_payload as encrypt_archive_payload,
)
from app.builtin_plugins.audit_export_archive.models import (
    FORMAT_JSONL,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    AuditArchiveBatch,
)
from app.core.errors import AUDIT_ARCHIVE_BUSY, ApiError
from app.core.ids import utc_now
from app.plugin_sdk import AuditQuery

_ACTION_ARCHIVED = "AUDIT_ARCHIVED"
_ACTION_FAILED = "AUDIT_ARCHIVE_FAILED"
_RESOURCE_TYPE = "AUDIT_ARCHIVE_BATCH"
_BREAKER = "plugin.audit_export_archive"
_ENCRYPTION_NONE = "none"
_ENCRYPTION_AES_GCM = "aes-256-gcm"

_LOGGER = logging.getLogger(__name__)


class AuditArchiveService:
    """归档批次：手动触发 + 定时调度（冷副本写对象存储）。

    幂等与互斥（v1.4 补齐）：
    - **窗口幂等**：定时窗口对齐到 UTC 天边界，同一窗口已有 SUCCEEDED 批次时直接复用，
      多副本各自调度不会产出重复冷副本；
    - **在途互斥**：同租户已有 PENDING/RUNNING 批次时拒绝新批次（409 `AI_AUDIT_ARCHIVE_BUSY`），
      单进程内再用 `_trigger_lock` 串行化（定时器与手动触发不会并发进入）；
    - **陈旧批次回收**：超过半个调度周期仍未终态的批次视为中断，落 FAILED 后放行新批次，
      避免"崩溃一次就永久 BUSY"。
    """

    def __init__(self, sdk: Any, config: AuditExportConfig, *, guard: Any = None) -> None:
        self._sdk = sdk
        self._config = config
        self._guard = guard
        self._timer: threading.Timer | None = None
        self._closed = False
        # 同一进程内的定时器与手动触发必须串行：否则两次 _execute 会同时读同一窗口
        self._trigger_lock = threading.Lock()

    # ---- 密钥 ----
    def _key_ring(self) -> ArchiveKeyRing | None:
        """装配密钥环（当前 + 上一把只读）。密钥问题必须在创建批次之前暴露。"""
        return build_key_ring(
            enabled=self._config.archive_encryption_enabled,
            secret_ref=self._config.archive_secret_ref,
            previous_secret_ref=self._config.archive_previous_secret_ref,
            provider=self._sdk.registry.try_get("secret.provider"),
        )

    # ---- 触发 ----
    def trigger(
        self,
        *,
        tenant_id: UUID,
        actor_user_id: UUID | None,
        lookback_hours: float | None = None,
        trigger_source: str = "MANUAL",
    ) -> dict[str, Any]:
        if self._guard is not None:
            self._guard.check(_BREAKER)
        window_start, window_end = self._window(lookback_hours=lookback_hours, trigger_source=trigger_source)
        key_ring = self._key_ring()  # 密钥缺失在创建批次前即失败，避免留下空批次
        with self._trigger_lock:
            reused = self._reserve_window(tenant_id=tenant_id, window_start=window_start, window_end=window_end)
            if reused is not None:
                return reused
            with self._sdk.session() as db:
                batch = AuditArchiveBatch(
                    tenant_id=tenant_id,
                    created_by=actor_user_id,
                    trigger_source=trigger_source,
                    status=STATUS_PENDING,
                    window_start=window_start,
                    window_end=window_end,
                    encryption=_ENCRYPTION_AES_GCM if key_ring is not None else _ENCRYPTION_NONE,
                )
                db.add(batch)
                db.flush()
                batch_id = batch.id
            self._execute(batch_id=batch_id, tenant_id=tenant_id, actor_user_id=actor_user_id, key_ring=key_ring)
            with self._sdk.session() as db:
                saved = db.get(AuditArchiveBatch, batch_id)
                if saved is None:  # pragma: no cover - 同实例内刚写入
                    raise ApiError("AI_AUDIT_ARCHIVE_FAILED", "audit archive batch disappeared", 502)
                return self._batch_dict(saved)

    def _window(self, *, lookback_hours: float | None, trigger_source: str) -> tuple[datetime, datetime]:
        """计算归档窗口。

        定时窗口对齐到 UTC 天边界：`utc_now()` 带微秒，不对齐的话每次调度都是"新窗口"，
        幂等复用永远命中不了，多副本必然各写一份冷副本。
        """
        window_end = utc_now() - timedelta(days=self._config.hot_retention_days)
        if trigger_source == "SCHEDULE":
            window_end = window_end.replace(hour=0, minute=0, second=0, microsecond=0)
        window_start = window_end - timedelta(hours=lookback_hours or self._config.archive_schedule_hours)
        return window_start, window_end

    def _reserve_window(self, *, tenant_id: UUID, window_start: Any, window_end: Any) -> dict[str, Any] | None:
        """窗口幂等 + 在途互斥 + 陈旧批次回收；返回可复用的已完成批次（无则 None）。"""
        now = utc_now()
        stale_before = now - timedelta(hours=max(1.0, self._config.archive_schedule_hours / 2))
        with self._sdk.session() as db:
            stale = db.scalars(
                select(AuditArchiveBatch).where(
                    AuditArchiveBatch.tenant_id == tenant_id,
                    AuditArchiveBatch.status.in_((STATUS_PENDING, STATUS_RUNNING)),
                    AuditArchiveBatch.created_at < stale_before,
                )
            ).all()
            for batch in stale:
                batch.status = STATUS_FAILED
                batch.error_code = "AI_AUDIT_ARCHIVE_FAILED"
                batch.error_message = "archive batch interrupted before completion"
                batch.finished_at = now
            if stale:
                db.flush()
            inflight = db.scalars(
                select(AuditArchiveBatch).where(
                    AuditArchiveBatch.tenant_id == tenant_id,
                    AuditArchiveBatch.status.in_((STATUS_PENDING, STATUS_RUNNING)),
                ).limit(1)
            ).first()
            if inflight is not None:
                raise ApiError(AUDIT_ARCHIVE_BUSY, "an audit archive batch is already running", 409)
            done = db.scalars(
                select(AuditArchiveBatch)
                .where(
                    AuditArchiveBatch.tenant_id == tenant_id,
                    AuditArchiveBatch.window_start == window_start,
                    AuditArchiveBatch.window_end == window_end,
                    AuditArchiveBatch.status == STATUS_SUCCEEDED,
                )
                .order_by(AuditArchiveBatch.created_at.desc())
                .limit(1)
            ).first()
            return self._batch_dict(done) if done is not None else None

    def list_runs(self, *, tenant_id: UUID, limit: int = 20) -> list[dict[str, Any]]:
        with self._sdk.session() as db:
            rows = db.scalars(
                select(AuditArchiveBatch)
                .where(AuditArchiveBatch.tenant_id == tenant_id)
                .order_by(AuditArchiveBatch.created_at.desc())
                .limit(max(1, min(limit, 100)))
            ).all()
            return [self._batch_dict(item) for item in rows]

    # ---- 执行 ----
    def _execute(
        self,
        *,
        batch_id: UUID,
        tenant_id: UUID,
        actor_user_id: UUID | None,
        key_ring: ArchiveKeyRing | None,
    ) -> None:
        # 写入恒用当前密钥；上一把密钥只参与解密，绝不参与加密（轮换后不再产生旧密钥对象）
        key = key_ring.write_key if key_ring is not None else None
        key_id = key_ring.write_key_id if key_ring is not None else None
        with self._sdk.session() as db:
            batch = db.get(AuditArchiveBatch, batch_id)
            if batch is None:
                return
            batch.status = STATUS_RUNNING
            batch.started_at = utc_now()
            window_start, window_end = batch.window_start, batch.window_end
            encryption = batch.encryption
            prefix = f"{self._config.archive_prefix}/{tenant_id}/{batch_id}"
            batch.object_prefix = prefix
        try:
            store = self._object_store()
            query = AuditQuery(
                tenant_id=tenant_id,
                start_time=window_start,
                end_time=window_end,
                page_size=min(self._config.chunk_rows, 2000),
            )
            buffer: list[dict[str, Any]] = []
            objects: list[dict[str, Any]] = []
            row_count = 0
            size_total = 0

            def flush() -> None:
                nonlocal buffer, size_total
                if not buffer:
                    return
                seq = len(objects) + 1
                payload = encode_shard(buffer, FORMAT_JSONL)
                digest = hashlib.sha256(payload).hexdigest()
                body = encrypt_archive_payload(payload, key) if key is not None else payload
                suffix = ".jsonl.enc" if key is not None else ".jsonl"
                object_key = f"{prefix}/part-{seq:05d}{suffix}"
                result = store.put(object_key, BytesIO(body), max_bytes=self._config.max_object_bytes)
                objects.append(
                    {
                        "seq": seq,
                        "object_key": object_key,
                        "rows": len(buffer),
                        "size_bytes": result.size_bytes,
                        "sha256": digest,
                        # 索引侧记密钥归属：轮换闸门只读索引即可判断"是否仍依赖旧密钥"
                        "key_id": key_id,
                    }
                )
                size_total += result.size_bytes
                buffer = []

            for record in self._sdk.read_audit(query):
                buffer.append(to_export_row(record, profile=self._config.redaction_profile, subject_key=self._config.redaction_key))
                row_count += 1
                if len(buffer) >= self._config.chunk_rows:
                    flush()
            flush()

            manifest = manifest_payload(
                job_id=str(batch_id),
                tenant_id=str(tenant_id),
                export_format=FORMAT_JSONL,
                filters={"window_start": window_start.isoformat(), "window_end": window_end.isoformat()},
                shards=objects,
                row_count=row_count,
                truncated=False,
                generated_at=utc_now(),
                key_id=key_id,
            )
            manifest_body = encrypt_archive_payload(manifest, key) if key is not None else manifest
            manifest_suffix = ".json.enc" if key is not None else ".json"
            manifest_result = store.put(f"{prefix}/manifest{manifest_suffix}", BytesIO(manifest_body), max_bytes=self._config.max_object_bytes)
            size_total += manifest_result.size_bytes
            digest_total = sha256_total([item["sha256"] for item in objects])

            with self._sdk.session() as db:
                batch = db.get(AuditArchiveBatch, batch_id)
                if batch is None:
                    return
                batch.status = STATUS_SUCCEEDED
                batch.rows = row_count
                batch.objects = objects
                batch.sha256_total = digest_total
                batch.size_total = size_total
                batch.finished_at = utc_now()
            if self._guard is not None:
                self._guard.record(_BREAKER, succeeded=True)
            try:
                self._sdk.record_audit(
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    action=_ACTION_ARCHIVED,
                    resource_type=_RESOURCE_TYPE,
                    resource_id=batch_id,
                    summary=f"audit archive completed ({row_count} rows, {len(objects)} objects)",
                    detail={
                        "row_count": row_count,
                        "objects": len(objects),
                        "sha256_total": digest_total,
                        "encryption": encryption,
                        # 只记密钥指纹：审计日志里绝不出现密钥材料
                        "key_id": key_id,
                    },
                )
            except Exception:  # noqa: BLE001 - 批次已 SUCCEEDED，审计写入失败不得反把批次判失败
                _LOGGER.warning("failed to record audit for archive batch %s", batch_id, exc_info=True)
        except ApiError as exc:
            self._fail(batch_id, tenant_id, actor_user_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - 归档失败必须落库，否则批次永久 RUNNING
            self._fail(batch_id, tenant_id, actor_user_id, "AI_AUDIT_ARCHIVE_FAILED", str(exc)[:500])

    def _fail(self, batch_id: UUID, tenant_id: UUID, actor_user_id: UUID | None, code: str, message: str) -> None:
        if self._guard is not None:
            self._guard.record(_BREAKER, succeeded=False)
        with self._sdk.session() as db:
            batch = db.get(AuditArchiveBatch, batch_id)
            if batch is None:
                return
            batch.status = STATUS_FAILED
            batch.error_code = code
            batch.error_message = (message or code)[:1000]
            batch.finished_at = utc_now()
        try:
            self._sdk.record_audit(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action=_ACTION_FAILED,
                resource_type=_RESOURCE_TYPE,
                resource_id=batch_id,
                summary=message or "audit archive failed",
                detail={"error_code": code},
            )
        except Exception:  # noqa: BLE001 - 批次已落 FAILED，审计写入失败不得掩盖原始失败原因
            _LOGGER.warning("failed to record audit for archive batch %s", batch_id, exc_info=True)

    def _object_store(self) -> Any:
        store = self._sdk.registry.try_get("object.store")
        if store is None:
            raise ApiError("AI_CAPABILITY_UNAVAILABLE", "object.store is not assembled", 503)
        return store

    # ---- 定时调度 ----
    def start_scheduler(self) -> None:
        """每日归档：启动后延迟一个周期才执行第一次，避免启动风暴。"""
        timer = threading.Timer(self._config.archive_schedule_hours * 3600, self._tick)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _tick(self) -> None:
        if self._closed:
            return
        try:
            self.run_for_all_tenants()
        finally:
            if not self._closed:
                self.start_scheduler()

    def shutdown(self) -> None:
        self._closed = True
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def run_for_all_tenants(self) -> int:
        # 租户枚举走内核只读缝（插件不 import app.models.Tenant）
        tenants = self._sdk.list_tenant_ids()
        done = 0
        for tenant_id in tenants:
            try:
                self.trigger(tenant_id=tenant_id, actor_user_id=None, trigger_source="SCHEDULE")
                done += 1
            except ApiError:
                continue
        return done

    @staticmethod
    def _batch_dict(batch: AuditArchiveBatch) -> dict[str, Any]:
        from app.core.servertime import iso_utc

        return {
            "id": str(batch.id),
            "status": batch.status,
            "trigger": batch.trigger_source,
            "window_start": iso_utc(batch.window_start),
            "window_end": iso_utc(batch.window_end),
            "rows": batch.rows,
            "objects": batch.objects or [],
            "sha256_total": batch.sha256_total,
            "size_total": batch.size_total,
            "encryption": batch.encryption,
            "error_code": batch.error_code,
            "error_message": batch.error_message,
            "created_at": iso_utc(batch.created_at),
            "started_at": iso_utc(batch.started_at),
            "finished_at": iso_utc(batch.finished_at),
        }
