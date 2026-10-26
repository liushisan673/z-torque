"""归档 / 导出对象对账：以插件自有索引表为唯一事实源，默认只读。

背景（v1.3 §12.5 第 4 条）：`object.store` 契约只有 `put` / `local_path` / `delete`，
**没有 `list`**，因此无法枚举存储侧对象。"这个对象还在不在"只能拿索引反查，不能正查。
对账口径如下（详见 v1.3 §12.8）。

索引源（均为插件自有表，索引即事实）
- 导出：`zr_audit_export_job.object_prefix` + `zr_audit_export_shard.object_key`
- 归档：`zr_audit_archive_batch.object_prefix` + `objects` JSON 清单

三类输出
1. `orphan`（孤儿对象，有键无任务行）：索引里写下过这个键，但已经没有任何**有效任务行**
   拥有它。两种成因：
   - 分片行的 `job_id` 在任务表里查不到（外键缺失 / 历史脏数据 / 跨库搬运）；
   - 任务行已是 FAILED 终态：`_mark_failed` 会回收分片与 manifest，但
     `recover_interrupted`（进程重启恢复）只改状态不回收，残留对象对任何 API 都不可达
     （`open_shard` 要求 SUCCEEDED）。
   只报"对象确实还在存储里"的键——已经被清掉的索引残留不必再删一次。
   `--apply` **只**删这一类，且删除前复核"是否被有效行引用"。
2. `dangling`（悬空引用）：任务行仍在，但它应产出的对象查不到。只对 SUCCEEDED 判定：
   PENDING/RUNNING 正在写、FAILED 本就不该有对象、热区已清理（`expires_at IS NULL`）的
   成功任务行是"按设计删掉了对象"，都不算悬空。
3. `invalid`（非法键）：键不满足 `{root}/{tenant}/{owner}/{leaf}` 规范，或租户 / 任务 /
   叶子（分片序号、格式、加密后缀）与所属行记录不一致 —— 越权前缀、手工拼装错误、
   跨租户搬运都属于这一类。`--apply` 不碰非法键（判定不了归属，删了就是猜）。

安全边界
- 默认 dry-run：唯一 I/O 是 SELECT 与 `object.store.local_path(...).exists()`，
  **不 delete、不写库、不写审计**。刻意不 import `app.db.session` —— 它在 import 期
  会对 SQLite 开发库做补列 ALTER，那已经是写入了。
- `--apply` 只删 `orphan` 里的键，逐条打印；单条删除失败只记录、不中断（退出码 2）。
- 不新增迁移，不动内核 Port 契约（`app/kernel/ports/*` 冻结）。
- 悬空检测依赖 `local_path()` 返回**真实本地路径**（社区版本地文件系统实现即如此）。
  企业版若把 `local_path` 实现成"下载到临时文件"，存在性判定恒真，悬空引用在该后端上
  检测不出来 —— 如实登记在 v1.3 §12.8，不用"扫存储"绕过去。

用法：
    python scripts/reconcile_audit_archive_objects.py                      # dry-run，全库
    python scripts/reconcile_audit_archive_objects.py --tenant-id <uuid>  # 限定租户
    python scripts/reconcile_audit_archive_objects.py --apply             # 删除孤儿对象
    python scripts/reconcile_audit_archive_objects.py --sample 50 --json  # 机器可读

退出码：0 = 三类均空；1 = 仍有待处理项；2 = 删除失败或探测异常。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 插件私有表（方案 b）：模型随插件目录，必须显式 import，否则 ORM 映射与 Base.metadata 里都没有它们
import app.builtin_plugins.audit_export_archive.models  # noqa: E402, F401
from app.builtin_plugins.audit_export_archive.config import (  # noqa: E402
    ARCHIVE_PREFIX,
    EXPORT_PREFIX,
)
from app.builtin_plugins.audit_export_archive.models import (  # noqa: E402
    FORMAT_CSV,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    AuditArchiveBatch,
    AuditExportJob,
    AuditExportShard,
)

CATEGORY_ORPHAN = "orphan"
CATEGORY_DANGLING = "dangling"
CATEGORY_INVALID = "invalid"

SOURCE_EXPORT_SHARD = "export_shard"
SOURCE_EXPORT_MANIFEST = "export_manifest"
SOURCE_ARCHIVE_OBJECT = "archive_object"
SOURCE_ARCHIVE_MANIFEST = "archive_manifest"

_ENCRYPTION_AES_GCM = "aes-256-gcm"

_UUID_SEGMENT = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_PART_LEAF = re.compile(r"^part-(\d{5})\.(jsonl|csv)(\.enc)?$")
_MANIFEST_LEAF = re.compile(r"^manifest\.json(\.enc)?$")


@dataclass(frozen=True)
class IndexEntry:
    """索引里一条"我声称写过这个键"的记录。"""

    source: str
    root: str
    object_key: str
    tenant_id: str | None
    owner_id: str | None
    # None 表示所属任务行不存在（有键无任务行）；否则为任务行状态
    owner_status: str | None
    # 规范要求的叶子文件名（分片带序号与格式后缀）；None 表示不校验
    expected_leaf: str | None = None


@dataclass(frozen=True)
class Finding:
    category: str
    object_key: str
    reason: str
    source: str
    tenant_id: str | None = None
    owner_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "object_key": self.object_key,
            "reason": self.reason,
            "source": self.source,
            "tenant_id": self.tenant_id,
            "owner_id": self.owner_id,
        }


@dataclass
class ReconcileReport:
    orphan: list[Finding] = field(default_factory=list)
    dangling: list[Finding] = field(default_factory=list)
    invalid: list[Finding] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    # 同名键同时被有效行引用（防御：绝不因为另一条孤儿记录把它删掉）
    protected_skipped: list[str] = field(default_factory=list)
    delete_errors: list[tuple[str, str]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            CATEGORY_ORPHAN: len(self.orphan),
            CATEGORY_DANGLING: len(self.dangling),
            CATEGORY_INVALID: len(self.invalid),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts(),
            "orphan": [item.as_dict() for item in self.orphan],
            "dangling": [item.as_dict() for item in self.dangling],
            "invalid": [item.as_dict() for item in self.invalid],
            "deleted": list(self.deleted),
            "protected_skipped": list(self.protected_skipped),
            "delete_errors": [{"object_key": key, "error": message} for key, message in self.delete_errors],
        }


def _normalize_root(prefix: str) -> str:
    return str(prefix or "").strip().strip("/")


def key_problem(entry: IndexEntry) -> str | None:
    """校验键是否满足「归档/导出前缀规范」；返回缺陷描述，合规返回 None。

    纯函数（不碰存储），因此非法键可以在任何探测之前被拦下 —— 探测一个越界键本身就是风险。
    """
    if not entry.object_key:
        return "index entry carries no object key"
    segments = entry.object_key.split("/")
    root_segments = [item for item in _normalize_root(entry.root).split("/") if item]
    if not root_segments:
        return "index entry has no configured object prefix"
    if len(segments) != len(root_segments) + 3:
        return f"key does not use the {entry.root}/<tenant>/<owner>/<leaf> layout"
    if segments[: len(root_segments)] != root_segments:
        return f"key is outside the {entry.root}/ prefix"
    tenant_segment, owner_segment, leaf = segments[-3], segments[-2], segments[-1]
    if _UUID_SEGMENT.match(tenant_segment) is None or _UUID_SEGMENT.match(owner_segment) is None:
        return "key tenant/owner segments are not UUIDs"
    if entry.tenant_id is not None and tenant_segment != entry.tenant_id:
        return "key tenant segment does not match the owning row"
    if entry.owner_id is not None and owner_segment != entry.owner_id:
        return "key owner segment does not match the owning row"
    if _PART_LEAF.match(leaf) is None and _MANIFEST_LEAF.match(leaf) is None:
        return "key leaf does not match the part/manifest naming rule"
    if entry.expected_leaf is not None and leaf != entry.expected_leaf:
        return f"key leaf {leaf!r} does not match the recorded {entry.expected_leaf!r}"
    return None


def evaluate(entries: Iterable[IndexEntry], *, exists: Callable[[str], bool]) -> ReconcileReport:
    """把索引条目判成三类发现。`exists` 只用于"对象是否还在"，不产生任何写入。"""
    report = ReconcileReport()
    for entry in entries:
        problem = key_problem(entry)
        if problem is not None:
            report.invalid.append(
                Finding(CATEGORY_INVALID, entry.object_key, problem, entry.source, entry.tenant_id, entry.owner_id)
            )
            continue
        if entry.owner_status is None or entry.owner_status == STATUS_FAILED:
            # 有键无（有效）任务行：只有对象确实还在时才值得报，已清掉的索引残留不制造噪音
            if not exists(entry.object_key):
                continue
            reason = (
                "indexed key has no owning task row"
                if entry.owner_status is None
                else "owning task is FAILED, object is unreachable"
            )
            report.orphan.append(
                Finding(CATEGORY_ORPHAN, entry.object_key, reason, entry.source, entry.tenant_id, entry.owner_id)
            )
            continue
        if entry.owner_status != STATUS_SUCCEEDED:
            # PENDING / RUNNING 正在写，不做存在性判定
            continue
        if not exists(entry.object_key):
            report.dangling.append(
                Finding(
                    CATEGORY_DANGLING,
                    entry.object_key,
                    "referenced object is missing from object.store",
                    entry.source,
                    entry.tenant_id,
                    entry.owner_id,
                )
            )
    return report


def protected_keys(entries: Iterable[IndexEntry]) -> set[str]:
    """被有效任务行（存在且未失败）引用的键集合：`--apply` 时作为删除护栏。"""
    return {
        entry.object_key
        for entry in entries
        if entry.object_key and entry.owner_status is not None and entry.owner_status != STATUS_FAILED
    }


def collect_export_entries(
    session: Session,
    *,
    export_prefix: str = EXPORT_PREFIX,
    tenant_id: UUID | None = None,
) -> list[IndexEntry]:
    root = _normalize_root(export_prefix)
    entries: list[IndexEntry] = []
    # LEFT JOIN 任务表：分片行在、任务行不在 → 有键无任务行（这正是外键缺位才可能出现的脏数据）
    stmt = select(AuditExportShard, AuditExportJob).outerjoin(
        AuditExportJob, AuditExportShard.job_id == AuditExportJob.id
    )
    if tenant_id is not None:
        stmt = stmt.where(AuditExportShard.tenant_id == tenant_id)
    for shard, job in session.execute(stmt).all():
        if job is None:
            entries.append(
                IndexEntry(
                    source=SOURCE_EXPORT_SHARD,
                    root=root,
                    object_key=shard.object_key,
                    tenant_id=str(shard.tenant_id),
                    owner_id=str(shard.job_id),
                    owner_status=None,
                    expected_leaf=None,
                )
            )
            continue
        extension = "csv" if job.export_format == FORMAT_CSV else "jsonl"
        entries.append(
            IndexEntry(
                source=SOURCE_EXPORT_SHARD,
                root=root,
                object_key=shard.object_key,
                tenant_id=str(job.tenant_id),
                owner_id=str(job.id),
                owner_status=job.status,
                expected_leaf=f"part-{shard.seq:05d}.{extension}",
            )
        )
    # manifest 键不在分片表里，只能由任务行自己记录的 object_prefix 推导
    job_stmt = select(AuditExportJob).where(AuditExportJob.object_prefix.is_not(None))
    if tenant_id is not None:
        job_stmt = job_stmt.where(AuditExportJob.tenant_id == tenant_id)
    for job in session.scalars(job_stmt).all():
        if job.status == STATUS_SUCCEEDED and job.expires_at is None:
            # 热区已清理：对象按设计删除、任务行保留用于追溯，不该判成悬空
            continue
        entries.append(
            IndexEntry(
                source=SOURCE_EXPORT_MANIFEST,
                root=root,
                object_key=f"{job.object_prefix}/manifest.json",
                tenant_id=str(job.tenant_id),
                owner_id=str(job.id),
                owner_status=job.status,
                expected_leaf="manifest.json",
            )
        )
    return entries


def collect_archive_entries(
    session: Session,
    *,
    archive_prefix: str = ARCHIVE_PREFIX,
    tenant_id: UUID | None = None,
) -> list[IndexEntry]:
    root = _normalize_root(archive_prefix)
    entries: list[IndexEntry] = []
    stmt = select(AuditArchiveBatch)
    if tenant_id is not None:
        stmt = stmt.where(AuditArchiveBatch.tenant_id == tenant_id)
    for batch in session.scalars(stmt).all():
        encrypted = batch.encryption == _ENCRYPTION_AES_GCM
        suffix = ".jsonl.enc" if encrypted else ".jsonl"
        for item in batch.objects or []:
            record = item if isinstance(item, dict) else {}
            key = str(record.get("object_key") or "")
            seq = record.get("seq")
            expected_leaf = f"part-{int(seq):05d}{suffix}" if isinstance(seq, int) else None
            entries.append(
                IndexEntry(
                    source=SOURCE_ARCHIVE_OBJECT,
                    root=root,
                    object_key=key,
                    tenant_id=str(batch.tenant_id),
                    owner_id=str(batch.id),
                    owner_status=batch.status,
                    expected_leaf=expected_leaf,
                )
            )
        # manifest 只有成功批次才有（失败批次连 objects 清单都不会落库）
        if batch.status == STATUS_SUCCEEDED and batch.object_prefix:
            leaf = "manifest.json.enc" if encrypted else "manifest.json"
            entries.append(
                IndexEntry(
                    source=SOURCE_ARCHIVE_MANIFEST,
                    root=root,
                    object_key=f"{batch.object_prefix}/{leaf}",
                    tenant_id=str(batch.tenant_id),
                    owner_id=str(batch.id),
                    owner_status=batch.status,
                    expected_leaf=leaf,
                )
            )
    return entries


def collect_entries(
    session: Session,
    *,
    export_prefix: str = EXPORT_PREFIX,
    archive_prefix: str = ARCHIVE_PREFIX,
    tenant_id: UUID | None = None,
) -> list[IndexEntry]:
    return [
        *collect_export_entries(session, export_prefix=export_prefix, tenant_id=tenant_id),
        *collect_archive_entries(session, archive_prefix=archive_prefix, tenant_id=tenant_id),
    ]


class ArchiveObjectReconciler:
    """对账执行器：`run(apply=False)` 只读，`run(apply=True)` 只删孤儿对象。"""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        object_store: Any,
        *,
        export_prefix: str = EXPORT_PREFIX,
        archive_prefix: str = ARCHIVE_PREFIX,
        tenant_id: UUID | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._object_store = object_store
        self._export_prefix = export_prefix
        self._archive_prefix = archive_prefix
        self._tenant_id = tenant_id

    def _exists(self, key: str) -> bool:
        # 契约里没有 exists/head：唯一合法的存在性探针是 local_path()（只读，不建目录）
        return Path(self._object_store.local_path(key)).exists()

    def collect(self) -> list[IndexEntry]:
        with self._session_factory() as session:
            return collect_entries(
                session,
                export_prefix=self._export_prefix,
                archive_prefix=self._archive_prefix,
                tenant_id=self._tenant_id,
            )

    def run(self, *, apply: bool = False) -> ReconcileReport:
        entries = self.collect()
        report = evaluate(entries, exists=self._exists)
        if not apply:
            return report
        protected = protected_keys(entries)
        seen: set[str] = set()
        for finding in report.orphan:
            key = finding.object_key
            if key in seen:
                continue
            seen.add(key)
            if key in protected:
                report.protected_skipped.append(key)
                continue
            try:
                self._object_store.delete(key)
            except Exception as exc:  # noqa: BLE001 - 单条删除失败不能中断整批对账
                report.delete_errors.append((key, str(exc)))
                continue
            report.deleted.append(key)
        return report


def _print_report(report: ReconcileReport, *, sample: int, applied: bool) -> None:
    # 输出保持纯 ASCII：Windows 控制台默认 GBK，中文样例路径会直接抛 UnicodeEncodeError
    print(f"mode={'apply' if applied else 'dry-run'}")
    for category, findings in (
        (CATEGORY_ORPHAN, report.orphan),
        (CATEGORY_DANGLING, report.dangling),
        (CATEGORY_INVALID, report.invalid),
    ):
        print(f"{category} count={len(findings)}")
        for item in findings[: max(0, sample)]:
            print(f"  - {item.object_key} [{item.source}] {item.reason}")
        if len(findings) > max(0, sample):
            print(f"  ... {len(findings) - max(0, sample)} more")
    print(f"deleted count={len(report.deleted)}")
    for key in report.deleted:
        print(f"  - {key}")
    if report.protected_skipped:
        print(f"protected_skipped count={len(report.protected_skipped)}")
        for key in report.protected_skipped:
            print(f"  - {key}")
    if report.delete_errors:
        print(f"delete_errors count={len(report.delete_errors)}")
        for key, message in report.delete_errors:
            print(f"  - {key} {message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile audit export/archive objects against the plugin's own index tables (read-only by default)."
    )
    parser.add_argument("--apply", action="store_true", help="delete orphan objects (default: dry-run, zero writes)")
    parser.add_argument("--sample", type=int, default=10, help="samples printed per category (default: 10)")
    parser.add_argument("--tenant-id", default=None, help="restrict reconciliation to one tenant")
    parser.add_argument("--archive-prefix", default=None, help="override AUDIT_ARCHIVE_PREFIX")
    parser.add_argument("--object-root", default=None, help="local object store root (default: FILE_STORAGE_DIR)")
    parser.add_argument("--database-url", default=None, help="override DATABASE_URL")
    parser.add_argument("--json", action="store_true", help="print a machine-readable report")
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.builtin_plugins.object_store import LocalObjectStore
    from app.config import get_settings

    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - 配置不全时给出可读结论，而不是栈回溯
        print(f"cannot load settings: {exc}", file=sys.stderr)
        return 2

    archive_prefix = _normalize_root(args.archive_prefix or getattr(settings, "audit_archive_prefix", ARCHIVE_PREFIX))
    object_root = Path(args.object_root) if args.object_root else Path(settings.file_storage_dir)
    tenant_id = UUID(args.tenant_id) if args.tenant_id else None

    engine = create_engine(args.database_url or settings.database_url, future=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    store = LocalObjectStore(object_root)
    reconciler = ArchiveObjectReconciler(
        session_factory,
        store,
        export_prefix=EXPORT_PREFIX,
        archive_prefix=archive_prefix,
        tenant_id=tenant_id,
    )
    try:
        report = reconciler.run(apply=args.apply)
    except Exception as exc:  # noqa: BLE001 - 存储/数据库探测失败按 2 退出，便于运维区分"有发现"与"跑不动"
        print(f"reconcile failed: {exc}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=True, indent=2))
    else:
        print(f"archive_prefix={archive_prefix} export_prefix={EXPORT_PREFIX}")
        _print_report(report, sample=args.sample, applied=args.apply)

    if report.delete_errors:
        return 2
    pending = len(report.dangling) + len(report.invalid) + len(report.protected_skipped)
    if pending:
        return 1
    if report.orphan and not args.apply:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
