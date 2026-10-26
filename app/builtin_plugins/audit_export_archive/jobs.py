"""导出任务：状态机 + 后台分片执行（v1.3 §5.1 / §5.2）。

状态机：PENDING → RUNNING → SUCCEEDED / FAILED（失败不自动重跑，需显式新建任务）。
并发模型：固定大小线程池（AUDIT_EXPORT_MAX_CONCURRENCY），每个任务在独立线程内
自开短事务，不共享请求会话。
多副本语义（v1.3 §5.9）：执行权靠行级租约仲裁——`_claim` 用一条条件 UPDATE
（`status=PENDING` 且租约空缺/已过期）抢占，`rowcount==1` 才算赢；执行中按
`AUDIT_EXPORT_LEASE_RENEW_INTERVAL_SECONDS` 续租，丢租立即中止；`recover_interrupted`
只回收"租约可判定为本实例或已过期"的行，绝不动其他副本在途的任务。

数据边界：审计事件只经 `sdk.read_audit` 读取；本模块只碰 `zr_audit_export_*` 两表。
观测边界：任务生命周期经 `observability.sink` 上报 `audit_export.job` 元信息（见
`AUDIT_EXPORT_EVENT_FIELDS` 白名单）；不含租户 id 明文、文件名、审计正文与脚本内容。
上报失败只记日志，绝不影响导出主流程（观测是旁路，不是前置条件）。
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select, update

from app.builtin_plugins.audit_export_archive.config import SELF_ACTION_PREFIX, AuditExportConfig
from app.builtin_plugins.audit_export_archive.exporter import (
    encode_shard,
    manifest_payload,
    sha256_total,
    to_export_row,
)
from app.builtin_plugins.audit_export_archive.models import (
    FORMAT_CSV,
    FORMAT_JSONL,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    AuditExportJob,
    AuditExportShard,
)
from app.core.errors import ApiError
from app.core.ids import utc_now
from app.plugin_sdk import AuditQuery
from app.plugin_sdk.observability import emit_event

_ACTION_EXPORTED = "AUDIT_EXPORTED"
_ACTION_FAILED = "AUDIT_EXPORT_FAILED"
_RESOURCE_TYPE = "AUDIT_EXPORT_JOB"
_BREAKER = "plugin.audit_export_archive"

_LOGGER = logging.getLogger(__name__)

# ---- observability.sink 事件契约（事件名 / 状态 / payload 白名单）----
# 事件名与字段集合是对外契约：测试与运维看板都按这些常量断言，不得随手改名。
AUDIT_EXPORT_EVENT = "audit_export.job"
# 非失败状态的 payload 键集合（error_code 只在失败时出现，避免"成功也有 error 字段"的歧义）
AUDIT_EXPORT_EVENT_FIELDS: tuple[str, ...] = (
    "job_id",
    "format",
    "status",
    "rows_written",
    "chunk_count",
    "duration_ms",
)
# 失败状态额外带 error_code（稳定错误码，非异常文案）
AUDIT_EXPORT_EVENT_FAILURE_FIELDS: tuple[str, ...] = (*AUDIT_EXPORT_EVENT_FIELDS, "error_code")

EVENT_STATUS_QUEUED = "queued"
EVENT_STATUS_RUNNING = "running"
EVENT_STATUS_SUCCEEDED = "succeeded"
EVENT_STATUS_FAILED = "failed"


def _emit_job_event(
    *,
    job_id: Any,
    export_format: str,
    status: str,
    rows_written: int = 0,
    chunk_count: int = 0,
    duration_ms: int = 0,
    error_code: str | None = None,
) -> None:
    """上报一次导出任务生命周期事件（payload 严格按白名单构造）。

    禁止放入租户 id 明文、文件名、审计正文、脚本内容：这些都可能被日志平台长期留存。
    sink 故障只记 warning——观测旁路不得让一次已成功的导出变成失败。
    """
    payload: dict[str, Any] = {
        "job_id": str(job_id),
        "format": str(export_format),
        "status": status,
        "rows_written": max(0, int(rows_written)),
        "chunk_count": max(0, int(chunk_count)),
        "duration_ms": max(0, int(duration_ms)),
    }
    if error_code:
        payload["error_code"] = str(error_code)
    try:
        emit_event(AUDIT_EXPORT_EVENT, payload)
    except Exception:  # noqa: BLE001 - 观测上报失败不得影响主流程
        _LOGGER.warning(
            "observability sink failed for event %s (job %s, status %s)",
            AUDIT_EXPORT_EVENT,
            payload["job_id"],
            status,
            exc_info=True,
        )


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError("AI_AUDIT_EXPORT_DENIED", f"invalid time filter: {value}", 403) from exc


def _parse_uuid(value: Any) -> UUID | None:
    if value in (None, ""):
        return None
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise ApiError("AI_AUDIT_EXPORT_DENIED", "invalid actor_user_id filter", 403) from exc


def _elapsed_ms(started: float) -> int:
    """单调时钟起点的毫秒耗时（负数不可能，但取整后统一夹到 0）。"""
    return max(0, int((time.monotonic() - started) * 1000))


class AuditExportService:
    """导出领域服务：创建/查询任务、分片写对象、下载取路径、过期清理。"""

    def __init__(self, sdk: Any, config: AuditExportConfig, *, guard: Any = None) -> None:
        self._sdk = sdk
        self._config = config
        self._guard = guard
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._closed = False
        # 多副本租约：实例标识必须进程间唯一（默认 host:pid:rand 已足够区分副本）。
        # 主机名取 platform.node()：本插件被红线禁止出现任何网络符号（含网络模块名）。
        self._instance_id = config.instance_id or f"{platform.node() or 'host'}:{os.getpid()}:{uuid4().hex[:8]}"
        self._lease_ttl = timedelta(seconds=config.lease_ttl_seconds)
        self._lease_renew_interval = timedelta(seconds=config.lease_renew_interval_seconds)
        # 续租节流用单调时钟（墙钟跳变会让"该续租"判定失真）
        self._last_renew: dict[str, float] = {}
        self._last_renew_lock = threading.Lock()

    # ---- 生命周期 ----
    def start(self) -> None:
        self.recover_interrupted()
        with self._lock:
            if self._executor is None and not self._closed:
                self._executor = ThreadPoolExecutor(max_workers=self._config.max_concurrency, thread_name_prefix="audit-export")

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            executor, self._executor = self._executor, None
        if executor is not None:
            # 后台任务不可中断（导出件写一半比任务超时更糟）；等待在途分片收尾
            executor.shutdown(wait=True, cancel_futures=False)

    def recover_interrupted(self) -> int:
        """进程重启恢复：只回收**租约可判定为本实例或已过期**的遗留 PENDING/RUNNING。

        判定规则（多副本安全，v1.3 §5.9）：
        - `lease_owner IS NULL`：无主（PENDING 从未开始，或迁移前的存量行）；
        - `lease_owner = 本实例`：本进程上一轮留下的在途任务；
        - `lease_expires_at < now`：持有者失联，租约已过期。

        其他副本持**有效租约**的行一律不动——单进程实现会把它们误判为失败，
        这正是本方法需要租约的原因。被回收的任务补发 `failed` 生命周期事件：
        外部视角它就是"这次导出失败了"，不补发会让看板永远停在 running。
        """
        now = utc_now()
        reclaimable = or_(
            AuditExportJob.lease_owner.is_(None),
            AuditExportJob.lease_expires_at.is_(None),
            AuditExportJob.lease_expires_at < now,
            AuditExportJob.lease_owner == self._instance_id,
        )
        with self._sdk.session() as db:
            rows = db.scalars(
                select(AuditExportJob).where(
                    AuditExportJob.status.in_((STATUS_PENDING, STATUS_RUNNING)),
                    reclaimable,
                )
            ).all()
            interrupted = [(row.id, row.export_format) for row in rows]
            for row in rows:
                row.status = STATUS_FAILED
                row.error_code = "AI_AUDIT_EXPORT_FAILED"
                row.error_message = "export interrupted by service restart"
                row.finished_at = now
                row.lease_owner = None
                row.lease_expires_at = None
        for job_id, export_format in interrupted:
            _emit_job_event(
                job_id=job_id,
                export_format=export_format,
                status=EVENT_STATUS_FAILED,
                error_code="AI_AUDIT_EXPORT_FAILED",
            )
        return len(interrupted)

    # ---- 端口 ----
    def _object_store(self) -> Any:
        store = self._sdk.registry.try_get("object.store")
        if store is None:
            raise ApiError("AI_CAPABILITY_UNAVAILABLE", "object.store is not assembled", 503)
        return store

    def _check_breaker(self) -> None:
        if self._guard is not None:
            self._guard.check(_BREAKER)

    def _record_breaker(self, *, succeeded: bool) -> None:
        if self._guard is not None:
            self._guard.record(_BREAKER, succeeded=succeeded)

    # ---- 创建 ----
    def create_job(
        self,
        *,
        tenant_id: UUID,
        actor_user_id: UUID,
        filters: dict[str, Any],
        export_format: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        export_format = str(export_format or FORMAT_JSONL).lower()
        if export_format not in (FORMAT_JSONL, FORMAT_CSV):
            raise ApiError("AI_AUDIT_EXPORT_DENIED", "format must be jsonl or csv", 403)
        if export_format not in self._config.export_formats:
            raise ApiError("AI_AUDIT_EXPORT_DENIED", f"format {export_format} is disabled by configuration", 403)
        self._check_breaker()
        # 先确认执行器可用再落库：否则 503 之后会留下一条永远 PENDING 的孤儿任务行，
        # 用户看到"提交失败"却在任务列表里多出一条假任务
        self._ensure_executor()
        # 范围在创建时定稿：执行阶段只按 filters 读取，不再接受外部放大条件
        raw_actions = filters.get("actions")
        normalized = {
            "actions": list(raw_actions) if raw_actions is not None else None,
            "actor_user_id": str(filters["actor_user_id"]) if filters.get("actor_user_id") else None,
            "resource_type": filters.get("resource_type") or None,
            "start_time": filters.get("start_time"),
            "end_time": filters.get("end_time"),
        }
        with self._sdk.session() as db:
            job = AuditExportJob(
                tenant_id=tenant_id,
                created_by=actor_user_id,
                status=STATUS_PENDING,
                export_format=export_format,
                query_json=normalized,
                expires_at=utc_now() + timedelta(days=self._config.hot_retention_days),
            )
            db.add(job)
            db.flush()
            job_id = job.id
        payload = self.get_job(tenant_id=tenant_id, job_id=job_id)
        # queued 先于入队上报：任务一旦接受就一定有一条 queued，避免外部看到"凭空 running"
        _emit_job_event(job_id=job_id, export_format=export_format, status=EVENT_STATUS_QUEUED)
        self._submit(job_id)
        return payload

    def _ensure_executor(self) -> ThreadPoolExecutor:
        """取在用的线程池；未 start 或已 shutdown 一律 503（不得静默接受任务）。"""
        with self._lock:
            if self._closed or self._executor is None:
                raise ApiError("AI_CAPABILITY_UNAVAILABLE", "audit export executor is not running", 503)
            return self._executor

    def _submit(self, job_id: UUID) -> None:
        self._ensure_executor().submit(self._run, str(job_id))

    # ---- 租约（多副本执行权仲裁，v1.3 §5.9）----
    def _claim(self, job_id: UUID) -> bool:
        """抢占执行权：唯一仲裁点是这一条条件 UPDATE。

        不能先 SELECT 看状态再 UPDATE——两个副本同时读到 PENDING 就会双双执行。
        `rowcount == 1` 表示本次 UPDATE 真的把某一行从"可抢"改成了"我持有"。
        """
        now = utc_now()
        with self._sdk.session() as db:
            result = db.execute(
                update(AuditExportJob)
                .where(
                    AuditExportJob.id == job_id,
                    AuditExportJob.status == STATUS_PENDING,
                    or_(
                        AuditExportJob.lease_owner.is_(None),
                        AuditExportJob.lease_expires_at.is_(None),
                        AuditExportJob.lease_expires_at < now,
                    ),
                )
                .values(
                    status=STATUS_RUNNING,
                    started_at=now,
                    lease_owner=self._instance_id,
                    lease_expires_at=now + self._lease_ttl,
                    attempts=AuditExportJob.attempts + 1,
                )
            )
            won = result.rowcount == 1
        if won:
            with self._last_renew_lock:
                self._last_renew[str(job_id)] = time.monotonic()
            _LOGGER.info("audit export job %s claimed by %s", job_id, self._instance_id)
        return won

    def _renew(self, job_id: UUID) -> None:
        """续租；丢租必须立刻中止。

        丢租意味着另一个副本已接管（我的租约过期了）。继续写分片会造成
        "两个实例同时往同一 job 追加 part-0000N"，产出不可复算的导出件。
        """
        now = utc_now()
        with self._sdk.session() as db:
            result = db.execute(
                update(AuditExportJob)
                .where(AuditExportJob.id == job_id, AuditExportJob.lease_owner == self._instance_id)
                .values(lease_expires_at=now + self._lease_ttl)
            )
            renewed = result.rowcount == 1
        if renewed:
            with self._last_renew_lock:
                self._last_renew[str(job_id)] = time.monotonic()
            return
        raise ApiError("AI_AUDIT_EXPORT_FAILED", "export job lease was lost to another instance", 409)

    def _maybe_renew(self, job_id: UUID) -> None:
        """按 `AUDIT_EXPORT_LEASE_RENEW_INTERVAL_SECONDS` 节流续租（分片边界调用即可）。"""
        key = str(job_id)
        with self._last_renew_lock:
            last = self._last_renew.get(key)
        if last is None or time.monotonic() - last < self._lease_renew_interval.total_seconds():
            return
        self._renew(job_id)

    def _forget_lease(self, job_id: UUID) -> None:
        with self._last_renew_lock:
            self._last_renew.pop(str(job_id), None)

    # ---- 执行 ----
    def _run(self, job_id: str) -> None:
        started = time.monotonic()
        uid = UUID(job_id)
        try:
            if not self._claim(uid):
                # 另一副本持有效租约，或任务已终态：不重复执行，也不写失败（对方负责收尾）
                _LOGGER.info(
                    "audit export job %s is not claimable on %s, skip",
                    job_id,
                    self._instance_id,
                )
                return
            self._execute(uid)
        except ApiError as exc:
            self._mark_failed(uid, exc.code, exc.message, duration_ms=_elapsed_ms(started))
        except Exception as exc:  # noqa: BLE001 - 后台线程必须吞掉一切，否则任务永久 RUNNING
            self._mark_failed(uid, "AI_AUDIT_EXPORT_FAILED", str(exc)[:500], duration_ms=_elapsed_ms(started))
        finally:
            self._forget_lease(uid)

    def _mark_failed(self, job_id: UUID, code: str, message: str, *, duration_ms: int = 0) -> None:
        self._record_breaker(succeeded=False)
        with self._sdk.session() as db:
            job = db.get(AuditExportJob, job_id)
            if job is None:
                return
            # 已丢租（另一副本接管）：不得把对方的任务标失败，也不得删它正在写的分片
            if job.lease_owner not in (None, self._instance_id):
                _LOGGER.warning(
                    "skip failing job %s on %s: lease owned by %s",
                    job_id,
                    self._instance_id,
                    job.lease_owner,
                )
                return
            # 失败任务不得留下"半份导出件"：已写分片与 manifest 一并回收，
            # 否则下载接口仍能取到不完整数据，用户无从判断导出是否完整。
            shard_rows = db.scalars(select(AuditExportShard).where(AuditExportShard.job_id == job_id)).all()
            orphan_keys = [row.object_key for row in shard_rows]
            for row in shard_rows:
                db.delete(row)
            orphan_keys.append(f"{job.object_prefix}/manifest.json")
            job.status = STATUS_FAILED
            job.error_code = code
            job.error_message = (message or code)[:1000]
            job.finished_at = utc_now()
            job.lease_owner = None
            job.lease_expires_at = None
            job.shards_total = 0
            job.rows_total = 0
            job.size_total = 0
            tenant_id, actor_id = job.tenant_id, job.created_by
            export_format = job.export_format
        try:
            store = self._object_store()
            for key in orphan_keys:
                store.delete(key)
        except Exception:  # noqa: BLE001 - 清理失败不能掩盖原始失败原因
            pass
        # 生命周期事件先于审计写入：观测看板要能立刻看到失败，不能等审计事务
        _emit_job_event(
            job_id=job_id,
            export_format=export_format,
            status=EVENT_STATUS_FAILED,
            duration_ms=duration_ms,
            error_code=code,
        )
        self._sdk.record_audit(
            tenant_id=tenant_id,
            actor_user_id=actor_id,
            action=_ACTION_FAILED,
            resource_type=_RESOURCE_TYPE,
            resource_id=job_id,
            summary=message or "audit export failed",
            detail={"error_code": code},
        )

    def _execute(self, job_id: UUID) -> None:
        with self._sdk.session() as db:
            job = db.get(AuditExportJob, job_id)
            # 抢到租约时状态与 owner 已由 `_claim` 写好；这里再校验一次，
            # 防止"抢到之后被判定失联并接管"的极端交错下继续产出分片
            if job is None or job.status != STATUS_RUNNING or job.lease_owner != self._instance_id:
                return
            tenant_id = job.tenant_id
            actor_id = job.created_by
            export_format = job.export_format
            filters = dict(job.query_json or {})
            prefix = f"{self._config.export_prefix}/{tenant_id}/{job_id}"
            job.object_prefix = prefix

        # 状态机已切 RUNNING：此刻起上报 running，耗时从这一刻算（不含排队等待）
        started = time.monotonic()
        _emit_job_event(
            job_id=job_id,
            export_format=export_format,
            status=EVENT_STATUS_RUNNING,
        )

        stored_actions = filters.get("actions")
        query = AuditQuery(
            tenant_id=tenant_id,
            actions=tuple(stored_actions) if stored_actions is not None else None,
            actor_user_id=_parse_uuid(filters.get("actor_user_id")),
            resource_type=filters.get("resource_type"),
            start_time=_parse_time(filters.get("start_time")),
            end_time=_parse_time(filters.get("end_time")),
            page_size=min(self._config.chunk_rows, 2000),
        )
        # 递归防护：未显式指定动作时排除 AUDIT_* 自身事件（可配置关闭，关闭时在审计里显式标记）
        skip_self = self._config.exclude_own_actions and stored_actions is None
        store = self._object_store()
        buffer: list[dict[str, Any]] = []
        shards: list[dict[str, Any]] = []
        row_count = 0
        size_total = 0
        extension = "csv" if export_format == FORMAT_CSV else "jsonl"

        def flush() -> None:
            nonlocal buffer, size_total
            # 分片边界是天然的续租点：导出再慢，最坏也只是每个分片续一次租
            self._maybe_renew(job_id)
            if not buffer:
                return
            seq = len(shards) + 1
            data = encode_shard(buffer, export_format)
            key = f"{prefix}/part-{seq:05d}.{extension}"
            digest = hashlib.sha256(data).hexdigest()
            # 单批重试：object_key 覆盖写 + (job_id, seq) 唯一，重试不会产生重复分片
            last_error: Exception | None = None
            for attempt in range(self._config.retry_limit + 1):
                try:
                    result = store.put(key, BytesIO(data), max_bytes=self._config.max_object_bytes)
                    break
                except Exception as exc:  # noqa: BLE001 - 分片写失败按退避重试，超限才判 Job 失败
                    last_error = exc
                    if attempt >= self._config.retry_limit:
                        raise ApiError("AI_AUDIT_EXPORT_FAILED", f"write shard #{seq} failed: {exc}") from exc
                    time.sleep(2**attempt)
            else:  # pragma: no cover - 循环必在 break 或 raise 结束
                raise ApiError("AI_AUDIT_EXPORT_FAILED", f"write shard #{seq} failed: {last_error}")
            with self._sdk.session() as db:
                db.add(
                    AuditExportShard(
                        tenant_id=tenant_id,
                        job_id=job_id,
                        seq=seq,
                        object_key=key,
                        rows=len(buffer),
                        size_bytes=result.size_bytes,
                        sha256=digest,
                    )
                )
                job = db.get(AuditExportJob, job_id)
                if job is not None:
                    job.shards_total = seq
            shards.append({"seq": seq, "object_key": key, "rows": len(buffer), "size_bytes": result.size_bytes, "sha256": digest})
            size_total += result.size_bytes
            buffer = []

        for record in self._sdk.read_audit(query):
            if skip_self and record.action.startswith(SELF_ACTION_PREFIX):
                continue
            if row_count >= self._config.max_rows:
                # 不隐式截断：超出上限直接失败，避免"少了几行却看起来成功"
                raise ApiError("AI_AUDIT_EXPORT_TOO_LARGE", f"export exceeds AUDIT_EXPORT_MAX_ROWS ({self._config.max_rows})", 413)
            buffer.append(to_export_row(record, profile=self._config.redaction_profile, subject_key=self._config.redaction_key))
            row_count += 1
            if len(buffer) >= self._config.chunk_rows:
                flush()
        flush()

        manifest_key = f"{prefix}/manifest.json"
        manifest = manifest_payload(
            job_id=str(job_id),
            tenant_id=str(tenant_id),
            export_format=export_format,
            filters=filters,
            shards=shards,
            row_count=row_count,
            truncated=False,
            recursive=not self._config.exclude_own_actions,
            generated_at=utc_now(),
        )
        manifest_result = store.put(manifest_key, BytesIO(manifest), max_bytes=self._config.max_object_bytes)
        size_total += manifest_result.size_bytes

        with self._sdk.session() as db:
            job = db.get(AuditExportJob, job_id)
            if job is None:
                return
            job.status = STATUS_SUCCEEDED
            job.rows_total = row_count
            job.shards_total = len(shards)
            job.size_total = size_total
            job.finished_at = utc_now()
            job.expires_at = utc_now() + timedelta(days=self._config.hot_retention_days)
            # 终态即释放租约：留着会让"是否还有实例在跑"的判断失真
            job.lease_owner = None
            job.lease_expires_at = None
        self._record_breaker(succeeded=True)
        _emit_job_event(
            job_id=job_id,
            export_format=export_format,
            status=EVENT_STATUS_SUCCEEDED,
            rows_written=row_count,
            chunk_count=len(shards),
            duration_ms=_elapsed_ms(started),
        )
        self._sdk.record_audit(
            tenant_id=tenant_id,
            actor_user_id=actor_id,
            action=_ACTION_EXPORTED,
            resource_type=_RESOURCE_TYPE,
            resource_id=job_id,
            summary=f"audit export completed ({row_count} rows, {len(shards)} shards)",
            detail={
                "row_count": row_count,
                "shard_count": len(shards),
                "sha256_total": sha256_total([item["sha256"] for item in shards]),
                "recursive": not self._config.exclude_own_actions,
            },
        )

    # ---- 查询/下载 ----
    def get_job(self, *, tenant_id: UUID, job_id: UUID) -> dict[str, Any]:
        with self._sdk.session() as db:
            job = db.get(AuditExportJob, job_id)
            if job is None or job.tenant_id != tenant_id:
                raise ApiError("AI_AUDIT_EXPORT_DENIED", "audit export job not found", 403)
            payload = self._job_dict(job)
            shards = db.scalars(select(AuditExportShard).where(AuditExportShard.job_id == job_id).order_by(AuditExportShard.seq.asc())).all()
            payload["shards"] = [self._shard_dict(item) for item in shards]
            return payload

    def list_jobs(self, *, tenant_id: UUID, limit: int = 20) -> list[dict[str, Any]]:
        with self._sdk.session() as db:
            rows = db.scalars(
                select(AuditExportJob)
                .where(AuditExportJob.tenant_id == tenant_id)
                .order_by(AuditExportJob.created_at.desc())
                .limit(max(1, min(limit, 100)))
            ).all()
            return [self._job_dict(item) for item in rows]

    def open_shard(self, *, tenant_id: UUID, job_id: UUID, seq: int, actor_user_id: UUID) -> tuple[Any, str]:
        """取分片本地路径；下载行为留痕（谁在什么时候取走了哪一段）。"""
        with self._sdk.session() as db:
            job = db.get(AuditExportJob, job_id)
            if job is None or job.tenant_id != tenant_id:
                raise ApiError("AI_AUDIT_EXPORT_DENIED", "audit export job not found", 403)
            if job.status != STATUS_SUCCEEDED:
                raise ApiError("AI_AUDIT_EXPORT_FAILED", "audit export job is not completed", 409)
        with self._sdk.session() as db:
            shard = db.scalars(select(AuditExportShard).where(AuditExportShard.job_id == job_id, AuditExportShard.seq == seq)).first()
            if shard is None:
                raise ApiError("AI_AUDIT_EXPORT_DENIED", "audit export shard not found", 403)
            key = shard.object_key
        store = self._object_store()
        path = store.local_path(key)
        if not path.exists():
            raise ApiError("AI_AUDIT_EXPORT_FAILED", "audit export shard has been purged", 410)
        self._sdk.record_audit(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="AUDIT_EXPORT_DOWNLOADED",
            resource_type=_RESOURCE_TYPE,
            resource_id=job_id,
            summary=f"audit export shard downloaded (#{seq})",
            detail={"seq": seq},
        )
        return path, path.name

    def purge_expired(self, *, limit: int = 100) -> int:
        """热窗口过期清理：删对象与分片记录，保留任务行用于审计追溯。"""
        store = self._object_store()
        now = datetime.now(timezone.utc)
        with self._sdk.session() as db:
            rows = db.scalars(
                select(AuditExportJob)
                .where(
                    AuditExportJob.status == STATUS_SUCCEEDED,
                    AuditExportJob.expires_at.is_not(None),
                    AuditExportJob.expires_at <= now,
                )
                .order_by(AuditExportJob.expires_at.asc())
                .limit(max(1, limit))
            ).all()
            purged = 0
            for job in rows:
                shards = db.scalars(select(AuditExportShard).where(AuditExportShard.job_id == job.id)).all()
                for shard in shards:
                    store.delete(shard.object_key)
                if job.object_prefix:
                    store.delete(f"{job.object_prefix}/manifest.json")
                db.execute(delete(AuditExportShard).where(AuditExportShard.job_id == job.id))
                job.shards_total = 0
                job.size_total = 0
                job.expires_at = None
                purged += 1
            return purged

    # ---- 序列化 ----
    @staticmethod
    def _job_dict(job: AuditExportJob) -> dict[str, Any]:
        from app.core.servertime import iso_utc

        return {
            "id": str(job.id),
            "status": job.status,
            "format": job.export_format,
            "filters": job.query_json or {},
            "rows_total": job.rows_total,
            "shards_total": job.shards_total,
            "size_total": job.size_total,
            "error_code": job.error_code,
            "error_message": job.error_message,
            "created_at": iso_utc(job.created_at),
            "started_at": iso_utc(job.started_at),
            "finished_at": iso_utc(job.finished_at),
            "expires_at": iso_utc(job.expires_at),
        }

    @staticmethod
    def _shard_dict(shard: AuditExportShard) -> dict[str, Any]:
        return {
            "seq": shard.seq,
            "object_key": shard.object_key,
            "rows": shard.rows,
            "size_bytes": shard.size_bytes,
            "sha256": shard.sha256,
        }
