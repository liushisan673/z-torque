"""归档 / 导出对象对账专项单测（v1.3 §12.8）。

对账脚本的事实源只能是插件自有索引表（`object.store` 契约没有 `list`），因此这里锁三件事：

1. 三类判定的边界：孤儿（有键无有效任务行）、悬空（任务行在、对象没了）、非法键；
2. dry-run 严格零写入：不删对象、不改库、不写审计；
3. `--apply` 只删孤儿对象：有效对象与非法键对象一律不动。

用例跑真实 `LocalObjectStore`（临时目录）+ 真实 `SessionLocal`，不用替身替代被测逻辑。
"""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.builtin_plugins.audit_export_archive.config import ARCHIVE_PREFIX, EXPORT_PREFIX
from app.builtin_plugins.audit_export_archive.models import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    AuditArchiveBatch,
    AuditExportJob,
    AuditExportShard,
)
from app.builtin_plugins.object_store import LocalObjectStore
from app.core.ids import utc_now
from app.db.session import SessionLocal
from app.models import AuditLog, Tenant
from scripts.reconcile_audit_archive_objects import (
    CATEGORY_DANGLING,
    CATEGORY_INVALID,
    CATEGORY_ORPHAN,
    SOURCE_ARCHIVE_MANIFEST,
    SOURCE_ARCHIVE_OBJECT,
    SOURCE_EXPORT_MANIFEST,
    SOURCE_EXPORT_SHARD,
    ArchiveObjectReconciler,
    IndexEntry,
    evaluate,
    key_problem,
)

_TENANT = "11111111-1111-4111-8111-111111111111"
_OTHER_TENANT = "33333333-3333-4333-8333-333333333333"
_OWNER = "22222222-2222-4222-8222-222222222222"


# ---------------------------------------------------------------- 纯函数判定


def _entry(key: str, **overrides: Any) -> IndexEntry:
    base: dict[str, Any] = {
        "source": SOURCE_EXPORT_SHARD,
        "root": EXPORT_PREFIX,
        "object_key": key,
        "tenant_id": _TENANT,
        "owner_id": _OWNER,
        "owner_status": STATUS_SUCCEEDED,
        "expected_leaf": "part-00001.jsonl",
    }
    base.update(overrides)
    return IndexEntry(**base)


def _export_key(leaf: str = "part-00001.jsonl", *, tenant: str = _TENANT, owner: str = _OWNER, root: str = EXPORT_PREFIX) -> str:
    return f"{root}/{tenant}/{owner}/{leaf}"


def test_key_problem_accepts_canonical_key() -> None:
    assert key_problem(_entry(_export_key())) is None


def test_key_problem_rejects_out_of_spec_keys() -> None:
    assert "outside" in str(key_problem(_entry(f"legacy-bucket/{_TENANT}/{_OWNER}/part-00001.jsonl")))
    assert "layout" in str(key_problem(_entry(f"{EXPORT_PREFIX}/{_TENANT}/{_OWNER}/nested/part-00001.jsonl")))
    assert "not UUIDs" in str(key_problem(_entry(f"{EXPORT_PREFIX}/tenant-x/{_OWNER}/part-00001.jsonl")))
    assert "naming rule" in str(key_problem(_entry(_export_key("part-00001.jsonl.bak"))))
    # 空键：归档批次 objects 清单里少了 object_key 字段
    assert "no object key" in str(key_problem(_entry("")))


def test_key_problem_detects_row_mismatch() -> None:
    # 租户段来自别的租户 = 跨租户搬运，必须判非法（不能只看"长得像"）
    assert "tenant segment" in str(key_problem(_entry(_export_key(tenant=_OTHER_TENANT))))
    # 任务段指向别处
    other_owner = "44444444-4444-4444-8444-444444444444"
    assert "owner segment" in str(key_problem(_entry(_export_key(owner=other_owner))))
    # 分片序号 / 格式与索引行记录不一致
    assert "does not match the recorded" in str(key_problem(_entry(_export_key("part-00002.jsonl"))))
    assert "does not match the recorded" in str(key_problem(_entry(_export_key("part-00001.csv"))))


def test_invalid_keys_are_never_probed() -> None:
    probed: list[str] = []
    entry = _entry(f"legacy-bucket/{_TENANT}/{_OWNER}/part-00001.jsonl")
    report = evaluate([entry], exists=lambda key: probed.append(key) or True)
    assert [item.category for item in report.invalid] == [CATEGORY_INVALID]
    assert probed == []
    assert report.orphan == [] and report.dangling == []


def test_orphan_requires_object_to_still_exist() -> None:
    # 有键无任务行：索引残留 + 对象还在 → 孤儿
    missing_owner = _entry(_export_key(), owner_id=None, tenant_id=None, owner_status=None, expected_leaf=None)
    assert [item.category for item in evaluate([missing_owner], exists=lambda _key: True).orphan] == [CATEGORY_ORPHAN]
    # 对象已经被清掉了：只剩索引噪音，不该再报（也不该再删一次）
    assert evaluate([missing_owner], exists=lambda _key: False).orphan == []


def test_failed_task_leftovers_are_orphan() -> None:
    entry = _entry(_export_key(), owner_status=STATUS_FAILED)
    report = evaluate([entry], exists=lambda _key: True)
    assert [item.category for item in report.orphan] == [CATEGORY_ORPHAN]
    assert "FAILED" in report.orphan[0].reason


def test_succeeded_task_with_missing_object_is_dangling() -> None:
    report = evaluate([_entry(_export_key())], exists=lambda _key: False)
    assert [item.category for item in report.dangling] == [CATEGORY_DANGLING]


@pytest.mark.parametrize("status", [STATUS_PENDING, STATUS_RUNNING])
def test_in_flight_tasks_are_not_judged(status: str) -> None:
    report = evaluate([_entry(_export_key(), owner_status=status)], exists=lambda _key: False)
    assert report.dangling == [] and report.orphan == [] and report.invalid == []


# ---------------------------------------------------------------- 数据库 + 存储


class _CountingStore:
    """包一层 LocalObjectStore，记录 delete 调用（dry-run 断言"零删除"就靠它）。"""

    def __init__(self, inner: LocalObjectStore) -> None:
        self._inner = inner
        self.deleted: list[str] = []

    def put(self, key: str, data: Any, *, max_bytes: int) -> Any:
        return self._inner.put(key, data, max_bytes=max_bytes)

    def local_path(self, key: str) -> Any:
        return self._inner.local_path(key)

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self._inner.delete(key)

    def exists(self, key: str) -> bool:
        return self._inner.local_path(key).exists()


def _tenant() -> UUID:
    tenant_id = uuid4()
    with SessionLocal.begin() as db:
        db.add(Tenant(id=tenant_id, name=f"t-{tenant_id}"))
    return tenant_id


def _put(store: Any, key: str) -> None:
    store.put(key, io.BytesIO(b'{"id": "row"}\n'), max_bytes=1024)


def _export_prefix(tenant_id: UUID, job_id: UUID) -> str:
    return f"{EXPORT_PREFIX}/{tenant_id}/{job_id}"


def _export_job(tenant_id: UUID, *, status: str, expires_at: Any = "auto") -> tuple[UUID, str]:
    """建一条导出任务行，返回 (job_id, object_prefix)：前缀与写入路径的拼法完全一致。"""
    job_id = uuid4()
    prefix = _export_prefix(tenant_id, job_id)
    if expires_at == "auto":
        expires_at = utc_now() + timedelta(days=90) if status == STATUS_SUCCEEDED else None
    with SessionLocal.begin() as db:
        db.add(
            AuditExportJob(
                id=job_id,
                tenant_id=tenant_id,
                status=status,
                query_json={},
                export_format="jsonl",
                object_prefix=prefix,
                expires_at=expires_at,
            )
        )
    return job_id, prefix


def _shard(tenant_id: UUID, job_id: UUID, seq: int, object_key: str) -> None:
    with SessionLocal.begin() as db:
        db.add(AuditExportShard(tenant_id=tenant_id, job_id=job_id, seq=seq, object_key=object_key))


def _reconciler(tenant_id: UUID, store: Any) -> ArchiveObjectReconciler:
    return ArchiveObjectReconciler(SessionLocal, store, tenant_id=tenant_id)


def _db_snapshot() -> tuple[int, int, int]:
    with SessionLocal() as db:
        return (
            db.scalar(select(func.count()).select_from(AuditExportJob)) or 0,
            db.scalar(select(func.count()).select_from(AuditExportShard)) or 0,
            db.scalar(select(func.count()).select_from(AuditLog)) or 0,
        )


def test_dry_run_is_zero_write(tmp_path) -> None:
    tenant_id = _tenant()
    store = _CountingStore(LocalObjectStore(tmp_path / "objects"))

    ok_job, ok_prefix = _export_job(tenant_id, status=STATUS_SUCCEEDED)
    _shard(tenant_id, ok_job, 1, f"{ok_prefix}/part-00001.jsonl")
    _put(store, f"{ok_prefix}/part-00001.jsonl")
    _put(store, f"{ok_prefix}/manifest.json")

    failed_job, failed_prefix = _export_job(tenant_id, status=STATUS_FAILED)
    _shard(tenant_id, failed_job, 1, f"{failed_prefix}/part-00001.jsonl")
    _put(store, f"{failed_prefix}/part-00001.jsonl")

    before = _db_snapshot()
    report = _reconciler(tenant_id, store).run(apply=False)

    assert [item.object_key for item in report.orphan] == [f"{failed_prefix}/part-00001.jsonl"]
    assert report.dangling == [] and report.invalid == []
    # 零写入：没删对象、没删/改行、没写审计
    assert store.deleted == []
    assert store.exists(f"{failed_prefix}/part-00001.jsonl") is True
    assert _db_snapshot() == before


def test_apply_deletes_only_orphan_objects(tmp_path) -> None:
    tenant_id = _tenant()
    store = _CountingStore(LocalObjectStore(tmp_path / "objects"))

    # 有效：SUCCEEDED 且分片 + manifest 都在
    ok_job, ok_prefix = _export_job(tenant_id, status=STATUS_SUCCEEDED)
    ok_key = f"{ok_prefix}/part-00001.jsonl"
    _shard(tenant_id, ok_job, 1, ok_key)
    _put(store, ok_key)
    _put(store, f"{ok_prefix}/manifest.json")

    # 孤儿：任务行 FAILED，分片与对象残留（recover_interrupted 路径不回收入口）
    failed_job, failed_prefix = _export_job(tenant_id, status=STATUS_FAILED)
    orphan_key = f"{failed_prefix}/part-00001.jsonl"
    _shard(tenant_id, failed_job, 1, orphan_key)
    _put(store, orphan_key)
    _put(store, f"{failed_prefix}/manifest.json")

    # 非法键：前缀不属于归档/导出命名空间，即使对象存在也不许删
    wrong_job, _wrong_prefix = _export_job(tenant_id, status=STATUS_SUCCEEDED)
    wrong_key = f"legacy-bucket/{tenant_id}/{wrong_job}/part-00001.jsonl"
    _shard(tenant_id, wrong_job, 1, wrong_key)
    _put(store, wrong_key)
    _put(store, f"{_export_prefix(tenant_id, wrong_job)}/manifest.json")

    # 悬空：任务行在、对象没了 → 报出来，但没东西可删
    gone_job, gone_prefix = _export_job(tenant_id, status=STATUS_SUCCEEDED)
    _shard(tenant_id, gone_job, 1, f"{gone_prefix}/part-00001.jsonl")
    _put(store, f"{gone_prefix}/manifest.json")

    report = _reconciler(tenant_id, store).run(apply=True)

    # FAILED 任务残留的分片与 manifest 都不可达，两条一起删（顺序按索引顺序：先分片后 manifest）
    assert set(report.deleted) == {orphan_key, f"{failed_prefix}/manifest.json"}
    assert set(store.deleted) == set(report.deleted)
    assert store.exists(orphan_key) is False
    assert store.exists(f"{failed_prefix}/manifest.json") is False
    assert store.exists(ok_key) is True
    assert store.exists(wrong_key) is True
    assert [item.object_key for item in report.invalid] == [wrong_key]
    assert [item.object_key for item in report.dangling] == [f"{gone_prefix}/part-00001.jsonl"]
    # 索引行一条不动：对账只碰对象，残留行留给人工裁决
    assert report.delete_errors == []


def test_missing_manifest_is_dangling_and_purged_job_is_skipped(tmp_path) -> None:
    tenant_id = _tenant()
    store = _CountingStore(LocalObjectStore(tmp_path / "objects"))

    # 分片在、manifest 没了 → 悬空（下载/校验链路会 404，属于真实可观测缺陷）
    broken_job, broken_prefix = _export_job(tenant_id, status=STATUS_SUCCEEDED)
    _shard(tenant_id, broken_job, 1, f"{broken_prefix}/part-00001.jsonl")
    _put(store, f"{broken_prefix}/part-00001.jsonl")

    # 热区已清理（成功任务、expires_at 置空、分片行已删）：不得报悬空、也不得报孤儿
    purged_job, purged_prefix = _export_job(tenant_id, status=STATUS_SUCCEEDED, expires_at=None)

    report = _reconciler(tenant_id, store).run(apply=False)
    dangling = {(item.object_key, item.source) for item in report.dangling}

    assert (f"{broken_prefix}/manifest.json", SOURCE_EXPORT_MANIFEST) in dangling
    assert not any(item.source == SOURCE_EXPORT_MANIFEST and str(purged_job) in item.object_key for item in report.dangling)
    assert not any(str(purged_job) in item.object_key for item in report.orphan)
    assert not str(purged_prefix) == ""


def test_archive_batch_objects_are_reconciled(tmp_path) -> None:
    tenant_id = _tenant()
    store = _CountingStore(LocalObjectStore(tmp_path / "objects"))

    with SessionLocal.begin() as db:
        batch = AuditArchiveBatch(
            tenant_id=tenant_id,
            status=STATUS_SUCCEEDED,
            trigger_source="MANUAL",
            window_start=utc_now() - timedelta(days=1),
            window_end=utc_now(),
            rows=10,
            objects=[],
            encryption="none",
        )
        db.add(batch)
        db.flush()
        batch_id = batch.id
        prefix = f"{ARCHIVE_PREFIX}/{tenant_id}/{batch_id}"
        batch.object_prefix = prefix
        # 与真实写入路径同样拼法：{root}/{tenant}/{batch}/part-NNNNN.jsonl
        batch.objects = [{"seq": 1, "object_key": f"{prefix}/part-00001.jsonl"}]

    report = _reconciler(tenant_id, store).run(apply=False)
    dangling = {(item.object_key, item.source) for item in report.dangling}

    # 归档对象与 manifest 都不在存储里 → 两条悬空
    assert (f"{prefix}/part-00001.jsonl", SOURCE_ARCHIVE_OBJECT) in dangling
    assert (f"{prefix}/manifest.json", SOURCE_ARCHIVE_MANIFEST) in dangling


def test_archive_object_key_from_other_tenant_is_invalid(tmp_path) -> None:
    tenant_id = _tenant()
    store = _CountingStore(LocalObjectStore(tmp_path / "objects"))

    with SessionLocal.begin() as db:
        batch = AuditArchiveBatch(
            tenant_id=tenant_id,
            status=STATUS_SUCCEEDED,
            trigger_source="MANUAL",
            window_start=utc_now() - timedelta(days=1),
            window_end=utc_now(),
            rows=1,
            objects=[{"seq": 1, "object_key": f"{ARCHIVE_PREFIX}/{_OTHER_TENANT}/{uuid4()}/part-00001.jsonl"}],
            encryption="none",
        )
        db.add(batch)
        db.flush()
        batch.object_prefix = f"{ARCHIVE_PREFIX}/{tenant_id}/{batch.id}"

    report = _reconciler(tenant_id, store).run(apply=False)
    invalid = [item for item in report.invalid if item.source == SOURCE_ARCHIVE_OBJECT]
    assert len(invalid) == 1
    assert "tenant segment" in invalid[0].reason
