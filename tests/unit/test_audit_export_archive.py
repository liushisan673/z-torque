"""审计导出 / 归档专项单测（v1.3 §5.1~§5.6）。

覆盖此前"只有代码、没有用例"的关键断言：
1. 分片 + manifest 的 sha256 可被外部独立复算（导出件是外部契约）；
2. 同条件重跑字节一致；已成功任务再执行不得重复产出分片；
3. 超限必须失败且**不产出** manifest（禁止静默截断）；
4. 递归防护：未显式指定动作时排除 `AUDIT_*`；
5. 冷副本明文 / AES-256-GCM 加解密往返与篡改检测；
6. 热窗口过期清理只删对象与分片记录，保留任务行；
7. `observability.sink` 接线：`audit_export.job` 生命周期事件的 payload 白名单与故障旁路。

用例全部跑真实 `AuditExportService` / `AuditArchiveService`：
会话走真实 SessionLocal，审计读取走真实 `iter_audit_events`，
对象写入走真实 `LocalObjectStore`（临时目录），只有 Port 注册表与 SDK 被替身化。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.builtin_plugins.audit_export_archive import jobs as audit_jobs
from app.builtin_plugins.audit_export_archive.archiver import (
    AuditArchiveService,
    _derive_key,
    decrypt_archive_payload,
    encrypt_archive_payload,
)
from app.builtin_plugins.audit_export_archive.config import CSV_COLUMNS, AuditExportConfig
from app.builtin_plugins.audit_export_archive.exporter import sha256_total, to_export_row
from app.builtin_plugins.audit_export_archive.jobs import (
    AUDIT_EXPORT_EVENT,
    AUDIT_EXPORT_EVENT_FAILURE_FIELDS,
    AUDIT_EXPORT_EVENT_FIELDS,
    AuditExportService,
)

# 插件私有表（方案 b）：模型随插件目录，测试按插件路径 import
from app.builtin_plugins.audit_export_archive.models import (
    AuditArchiveBatch,
    AuditExportJob,
    AuditExportShard,
)
from app.builtin_plugins.object_store import LocalObjectStore
from app.core.errors import ApiError
from app.core.ids import utc_now
from app.core.redaction import REDACTED
from app.db.session import SessionLocal
from app.kernel.audit.reader import AuditQuery, iter_audit_events
from app.models import AuditLog, Tenant, User
from tests.support.fakes import FakeRegistry, FakeSdk, FakeSecretProvider

_ARCHIVE_KEY = "s3cr3t-for-tests"


# ---------------------------------------------------------------- 基础设施


def _tenant_and_user() -> tuple:
    tenant_id = uuid4()
    with SessionLocal.begin() as db:
        db.add(Tenant(id=tenant_id, name=f"t-{tenant_id}"))
        db.flush()
        user = User(tenant_id=tenant_id, username=f"u-{uuid4()}", password_hash="x")
        db.add(user)
        db.flush()
        user_id = user.id
    return tenant_id, user_id


def _seed_audit(tenant_id, entries: list[dict]) -> None:
    with SessionLocal.begin() as db:
        for entry in entries:
            db.add(
                AuditLog(
                    tenant_id=tenant_id,
                    action=entry.get("action", "AGENT_CREATED"),
                    resource_type=entry.get("resource_type", "AGENT"),
                    resource_id=entry.get("resource_id"),
                    actor_user_id=entry.get("actor_user_id"),
                    summary=entry.get("summary", "s"),
                    detail=entry.get("detail"),
                    created_at=entry.get("created_at") or utc_now(),
                )
            )


def _config(**overrides) -> AuditExportConfig:
    base = dict(
        max_rows=500,
        chunk_rows=2,
        max_concurrency=2,
        retry_limit=0,
        hot_retention_days=90,
        archive_encryption_enabled=False,
        archive_schedule_hours=24.0,
        export_formats=("jsonl", "csv"),
    )
    base.update(overrides)
    return AuditExportConfig(**base)


def _service(tmp_path, *, config=None, secret=None, guard=None):
    ports = {"object.store": LocalObjectStore(tmp_path / "objects")}
    if secret is not None:
        ports["secret.provider"] = secret
    sdk = FakeSdk(settings=SimpleNamespace(), registry=FakeRegistry(ports))
    service = AuditExportService(sdk, config or _config(), guard=guard)
    service.start()
    return service, ports["object.store"]


def _archive_service(tmp_path, *, config=None, secret=None):
    ports = {"object.store": LocalObjectStore(tmp_path / "objects")}
    if secret is not None:
        ports["secret.provider"] = secret
    sdk = FakeSdk(settings=SimpleNamespace(), registry=FakeRegistry(ports))
    return AuditArchiveService(sdk, config or _config(), guard=None), ports["object.store"]


def _wait_job(service, tenant_id, job_id, *, timeout=15.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = service.get_job(tenant_id=tenant_id, job_id=job_id)
        except OperationalError:  # 后台线程写分片时 sqlite 可能短暂持锁
            time.sleep(0.05)
            continue
        if last["status"] in ("SUCCEEDED", "FAILED"):
            return last
        time.sleep(0.02)
    raise AssertionError(f"export job did not finish in time: {last}")


def _job_row(job_id) -> AuditExportJob:
    with SessionLocal() as db:
        return db.get(AuditExportJob, job_id)


def _store_bytes(store, key: str) -> bytes:
    return store.local_path(key).read_bytes()


# ---------------------------------------------------------------- 导出


def test_jsonl_export_produces_verifiable_shards_and_manifest(tmp_path):
    tenant_id, user_id = _tenant_and_user()
    _seed_audit(
        tenant_id,
        [
            {"action": "AGENT_CREATED"},
            {"action": "AGENT_UPDATED"},
            {"action": "SKILL_CREATED", "resource_type": "SKILL"},
            {"action": "SKILL_EXECUTED", "resource_type": "SKILL"},
            {"action": "FILE_UPLOADED", "resource_type": "FILE"},
        ],
    )

    service, store = _service(tmp_path)
    try:
        created = service.create_job(
            tenant_id=tenant_id, actor_user_id=user_id, filters={}, export_format="jsonl"
        )
        payload = _wait_job(service, tenant_id, created["id"])
    finally:
        service.shutdown()

    assert payload["status"] == "SUCCEEDED", payload
    assert payload["rows_total"] == 5
    assert payload["shards_total"] == 3, "chunk_rows=2 + 5 行 → 2/2/1 三片"

    prefix = _job_row(created["id"]).object_prefix
    manifest = json.loads(_store_bytes(store, f"{prefix}/manifest.json").decode("utf-8"))

    assert manifest["schema"] == "z-torque.audit-export.v1"
    assert manifest["row_count"] == 5
    assert manifest["truncated"] is False
    assert manifest["recursive"] is False
    assert [item["rows"] for item in manifest["shards"]] == [2, 2, 1]

    digests: list[str] = []
    exported_actions: list[str] = []
    for shard in manifest["shards"]:
        body = _store_bytes(store, shard["object_key"])
        digest = hashlib.sha256(body).hexdigest()
        assert digest == shard["sha256"], "manifest 分片摘要必须与对象字节一致"
        assert len(body) == shard["size_bytes"]
        digests.append(digest)
        exported_actions.extend(json.loads(line)["action"] for line in body.decode("utf-8").splitlines())

    # 外部校验方只凭 manifest 就能复算总摘要
    assert manifest["sha256_total"] == sha256_total(digests)
    assert sorted(exported_actions) == ["AGENT_CREATED", "AGENT_UPDATED", "FILE_UPLOADED", "SKILL_CREATED", "SKILL_EXECUTED"]

    # DB 分片索引与 manifest 一一对应（seq 连续、无重复）
    with SessionLocal() as db:
        rows = db.scalars(
            select(AuditExportShard).where(AuditExportShard.job_id == created["id"]).order_by(AuditExportShard.seq.asc())
        ).all()
        assert [item.seq for item in rows] == [1, 2, 3]
        assert [item.sha256 for item in rows] == digests


def test_csv_export_keeps_declared_column_order(tmp_path):
    tenant_id, user_id = _tenant_and_user()
    _seed_audit(tenant_id, [{"action": "AGENT_CREATED", "summary": "hello"}])

    service, store = _service(tmp_path, config=_config(chunk_rows=5))
    try:
        created = service.create_job(
            tenant_id=tenant_id, actor_user_id=user_id, filters={}, export_format="csv"
        )
        payload = _wait_job(service, tenant_id, created["id"])
    finally:
        service.shutdown()

    assert payload["status"] == "SUCCEEDED", payload
    prefix = _job_row(created["id"]).object_prefix
    rows = list(csv.reader(io.StringIO(_store_bytes(store, f"{prefix}/part-00001.csv").decode("utf-8"))))

    assert tuple(rows[0]) == CSV_COLUMNS, "CSV 列顺序是外部契约，不得漂移"
    assert len(rows) == 2
    assert rows[1][0] and rows[1][3] == "AGENT_CREATED"


def test_rerun_is_byte_identical_and_succeeded_job_is_never_reexecuted(tmp_path):
    tenant_id, user_id = _tenant_and_user()
    _seed_audit(tenant_id, [{"action": "AGENT_CREATED"}, {"action": "AGENT_UPDATED"}])

    service, store = _service(tmp_path)
    try:
        first = _wait_job(service, tenant_id, service.create_job(
            tenant_id=tenant_id, actor_user_id=user_id, filters={}, export_format="jsonl"
        )["id"])
        second = _wait_job(service, tenant_id, service.create_job(
            tenant_id=tenant_id, actor_user_id=user_id, filters={}, export_format="jsonl"
        )["id"])

        first_prefix = _job_row(first["id"]).object_prefix
        second_prefix = _job_row(second["id"]).object_prefix
        first_manifest = json.loads(_store_bytes(store, f"{first_prefix}/manifest.json").decode("utf-8"))
        second_manifest = json.loads(_store_bytes(store, f"{second_prefix}/manifest.json").decode("utf-8"))

        # 幂等性：相同范围重跑，分片字节完全一致（只有 job_id / generated_at 不同）
        assert [item["sha256"] for item in first_manifest["shards"]] == [item["sha256"] for item in second_manifest["shards"]]
        assert first_manifest["sha256_total"] == second_manifest["sha256_total"]
        assert first_manifest["row_count"] == second_manifest["row_count"] == 2

        # 已终态任务被再次调度时直接返回，不得追加分片或改状态
        with SessionLocal() as db:
            before = len(db.scalars(select(AuditExportShard).where(AuditExportShard.job_id == first["id"])).all())
        service._execute(first["id"])
        with SessionLocal() as db:
            after = len(db.scalars(select(AuditExportShard).where(AuditExportShard.job_id == first["id"])).all())
        assert before == after
        assert _job_row(first["id"]).status == "SUCCEEDED"
    finally:
        service.shutdown()


def test_over_limit_fails_loudly_and_never_emits_truncated_artifact(tmp_path):
    tenant_id, user_id = _tenant_and_user()
    _seed_audit(tenant_id, [{"action": "AGENT_CREATED"} for _ in range(5)])

    service, store = _service(tmp_path, config=_config(max_rows=2, chunk_rows=2))
    try:
        created = service.create_job(
            tenant_id=tenant_id, actor_user_id=user_id, filters={}, export_format="jsonl"
        )
        payload = _wait_job(service, tenant_id, created["id"])
    finally:
        service.shutdown()

    assert payload["status"] == "FAILED"
    assert payload["error_code"] == "AI_AUDIT_EXPORT_TOO_LARGE"
    assert payload["rows_total"] == 0, "失败任务不得留下'看起来成功'的行数"

    prefix = _job_row(created["id"]).object_prefix
    assert not store.local_path(f"{prefix}/manifest.json").exists(), "超限不得产出 manifest，避免被当成完整导出件"

    with SessionLocal() as db:
        failed = db.scalars(
            select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == "AUDIT_EXPORT_FAILED")
        ).all()
        assert len(failed) == 1
        assert failed[0].detail["error_code"] == "AI_AUDIT_EXPORT_TOO_LARGE"


def test_recursive_self_actions_are_excluded_unless_explicitly_requested(tmp_path):
    tenant_id, user_id = _tenant_and_user()
    _seed_audit(
        tenant_id,
        [
            {"action": "AGENT_CREATED"},
            {"action": "AUDIT_EXPORTED", "resource_type": "AUDIT_EXPORT_JOB"},
        ],
    )

    service, store = _service(tmp_path, config=_config(chunk_rows=10))
    try:
        implicit = _wait_job(service, tenant_id, service.create_job(
            tenant_id=tenant_id, actor_user_id=user_id, filters={}, export_format="jsonl"
        )["id"])
        explicit = _wait_job(service, tenant_id, service.create_job(
            tenant_id=tenant_id,
            actor_user_id=user_id,
            filters={"actions": ["AUDIT_EXPORTED"]},
            export_format="jsonl",
        )["id"])
    finally:
        service.shutdown()

    implicit_body = _store_bytes(store, f"{_job_row(implicit['id']).object_prefix}/part-00001.jsonl").decode("utf-8")
    explicit_body = _store_bytes(store, f"{_job_row(explicit['id']).object_prefix}/part-00001.jsonl").decode("utf-8")

    assert implicit["rows_total"] == 1
    assert "AUDIT_EXPORTED" not in implicit_body
    # 显式指定动作时不得再被"递归防护"吞掉（否则运维无法导出自身事件）；
    # 此时前一个成功任务留下的 AUDIT_EXPORTED 也在范围内，故断言"只含自身事件且非空"
    explicit_rows = [json.loads(line) for line in explicit_body.splitlines()]
    assert {item["action"] for item in explicit_rows} == {"AUDIT_EXPORTED"}
    assert explicit["rows_total"] == len(explicit_rows) >= 1
    assert all(item["resource_type"] == "AUDIT_EXPORT_JOB" for item in explicit_rows)


def test_redaction_sanitizes_detail_and_summary_and_strict_hashes_subjects(tmp_path):
    tenant_id, user_id = _tenant_and_user()
    _seed_audit(
        tenant_id,
        [
            {
                "action": "SKILL_EXECUTED",
                "resource_type": "SKILL",
                "resource_id": str(uuid4()),
                "actor_user_id": user_id,
                "summary": "call failed: secret://prod/db/password, token=abc123",
                "detail": {"api_key": "plain-key", "nested": {"password": "p"}, "safe": "keep"},
            }
        ],
    )

    [record] = list(iter_audit_events(FakeSdk().session, AuditQuery(tenant_id=tenant_id)))
    standard = to_export_row(record, profile="standard")
    strict = to_export_row(record, profile="strict", subject_key=_ARCHIVE_KEY)

    assert standard["summary"].startswith("call failed: [REDACTED]")
    assert "abc123" not in standard["summary"]
    assert standard["detail"]["api_key"] == REDACTED
    assert standard["detail"]["nested"]["password"] == REDACTED
    assert standard["detail"]["safe"] == "keep"
    assert standard["actor_user_id"] == str(user_id)

    assert strict["actor_user_id"].startswith("hmac:")
    assert strict["resource_id"].startswith("hmac:")
    assert str(user_id) not in strict["actor_user_id"]


def test_large_export_keeps_every_row_across_shards(tmp_path):
    """长导出（多分片）：行数守恒、seq 连续、总摘要可复算，不得中途丢行或重复。"""
    tenant_id, user_id = _tenant_and_user()
    expected_actions = [f"EVENT_{index:04d}" for index in range(150)]
    _seed_audit(tenant_id, [{"action": action} for action in expected_actions])

    service, store = _service(tmp_path, config=_config(chunk_rows=40, max_rows=1000))
    try:
        payload = _wait_job(service, tenant_id, service.create_job(
            tenant_id=tenant_id, actor_user_id=user_id, filters={}, export_format="jsonl"
        )["id"], timeout=30.0)
    finally:
        service.shutdown()

    assert payload["status"] == "SUCCEEDED", payload
    assert payload["rows_total"] == 150
    assert payload["shards_total"] == 4, "150 行 / 每片 40 行 → 40/40/40/30"

    prefix = _job_row(payload["id"]).object_prefix
    manifest = json.loads(_store_bytes(store, f"{prefix}/manifest.json").decode("utf-8"))
    assert [shard["rows"] for shard in manifest["shards"]] == [40, 40, 40, 30]
    assert [shard["seq"] for shard in manifest["shards"]] == [1, 2, 3, 4], "seq 必须连续且从 1 起，分片缺失可被发现"

    exported: list[str] = []
    digests: list[str] = []
    for shard in manifest["shards"]:
        body = _store_bytes(store, shard["object_key"])
        digests.append(hashlib.sha256(body).hexdigest())
        exported.extend(json.loads(line)["action"] for line in body.decode("utf-8").splitlines())

    assert sorted(exported) == sorted(expected_actions), "分片边界处不得丢行或重复写"
    assert manifest["sha256_total"] == sha256_total(digests)
    assert payload["size_total"] == sum(shard["size_bytes"] for shard in manifest["shards"]) + len(
        _store_bytes(store, f"{prefix}/manifest.json")
    ), "size_total 必须等于分片 + manifest 的真实字节数"


def test_recover_interrupted_marks_leftovers_failed_and_never_resumes_them(tmp_path):
    """重启恢复：上次进程遗留的 PENDING/RUNNING 一律 FAILED，且不得被自动重跑。

    单进程语义下不可能还有别的执行者，留 "RUNNING" 就是假运行态：轮询方永远等不到终态。
    """
    tenant_id, user_id = _tenant_and_user()
    _seed_audit(tenant_id, [{"action": "AGENT_CREATED"}])

    service, store = _service(tmp_path, config=_config(chunk_rows=10))
    try:
        done = _wait_job(service, tenant_id, service.create_job(
            tenant_id=tenant_id, actor_user_id=user_id, filters={}, export_format="jsonl"
        )["id"])
    finally:
        service.shutdown()

    # 模拟进程被杀时留下的在途任务
    with SessionLocal.begin() as db:
        leftovers = []
        for status in ("PENDING", "RUNNING"):
            row = AuditExportJob(
                tenant_id=tenant_id,
                created_by=user_id,
                status=status,
                export_format="jsonl",
                query_json={},
                started_at=utc_now() if status == "RUNNING" else None,
            )
            db.add(row)
            db.flush()
            leftovers.append(row.id)

    sdk = FakeSdk(settings=SimpleNamespace(), registry=FakeRegistry({"object.store": store}))
    restarted = AuditExportService(sdk, _config(chunk_rows=10))
    assert restarted.recover_interrupted() == 2
    restarted.start()  # start 内部也做一次恢复，此时已无遗留任务
    assert restarted.recover_interrupted() == 0, "恢复必须幂等，重复调用不能再次计数"
    try:
        for job_id in leftovers:
            payload = restarted.get_job(tenant_id=tenant_id, job_id=job_id)
            assert payload["status"] == "FAILED"
            assert payload["error_code"] == "AI_AUDIT_EXPORT_FAILED"
            assert payload["error_message"] == "export interrupted by service restart"
            assert payload["finished_at"] is not None, "终态必须有结束时间，否则外部无法判定超时"
            assert payload["shards"] == [], "遗留任务本就没有完整导出件，不得伪造分片"
        # 已成功任务不受恢复流程影响
        assert restarted.get_job(tenant_id=tenant_id, job_id=done["id"])["status"] == "SUCCEEDED"
    finally:
        restarted.shutdown()

    # 没有把遗留任务"重跑"成新导出件：对象目录下只有那条成功任务的分片
    with SessionLocal() as db:
        touched = db.scalars(select(AuditExportShard).where(AuditExportShard.job_id.in_(leftovers))).all()
        assert touched == []


def test_submit_after_shutdown_is_rejected_without_leaving_orphan_job(tmp_path):
    """关停后提交必须 503，且不得先落一条永远 PENDING 的孤儿任务行。"""
    tenant_id, user_id = _tenant_and_user()

    service, _ = _service(tmp_path, config=_config())
    service.shutdown()

    with pytest.raises(ApiError) as excinfo:
        service.create_job(tenant_id=tenant_id, actor_user_id=user_id, filters={}, export_format="jsonl")
    assert excinfo.value.code == "AI_CAPABILITY_UNAVAILABLE"

    with SessionLocal() as db:
        rows = db.scalars(select(AuditExportJob).where(AuditExportJob.tenant_id == tenant_id)).all()
        assert rows == [], "提交失败不能在任务列表里留下假任务"


def test_concurrent_exports_share_bounded_pool_without_cross_talk(tmp_path):
    """并发导出：线程池大小受配置约束，各任务分片互不串写、摘要可独立复算。"""
    tenant_id, user_id = _tenant_and_user()
    _seed_audit(tenant_id, [{"action": "AGENT_CREATED"} for _ in range(6)])

    service, store = _service(tmp_path, config=_config(chunk_rows=2, max_concurrency=2))
    try:
        jobs = [
            service.create_job(tenant_id=tenant_id, actor_user_id=user_id, filters={}, export_format="jsonl")
            for _ in range(4)
        ]
        payloads = [_wait_job(service, tenant_id, item["id"]) for item in jobs]
    finally:
        service.shutdown()

    assert all(item["status"] == "SUCCEEDED" for item in payloads), payloads
    assert all(item["rows_total"] == 6 and item["shards_total"] == 3 for item in payloads)

    prefixes = [_job_row(item["id"]).object_prefix for item in payloads]
    assert len(set(prefixes)) == len(prefixes), "分片前缀必须按 job 隔离，不能复用同一目录"

    totals = set()
    for prefix in prefixes:
        manifest = json.loads(_store_bytes(store, f"{prefix}/manifest.json").decode("utf-8"))
        assert manifest["row_count"] == 6
        keys = [shard["object_key"] for shard in manifest["shards"]]
        assert all(key.startswith(prefix) for key in keys), "任务不得把分片写到别人的前缀下"
        digests = [hashlib.sha256(_store_bytes(store, key)).hexdigest() for key in keys]
        assert digests == [shard["sha256"] for shard in manifest["shards"]]
        totals.add(manifest["sha256_total"])

    # 同范围重跑内容一致：并发下也不能因共享缓冲区而互相污染
    assert len(totals) == 1


# ---------------------------------------------------------------- 归档


def test_plaintext_archive_covers_hot_window_only(tmp_path):
    tenant_id, user_id = _tenant_and_user()
    now = utc_now()
    _seed_audit(tenant_id, [
        {"created_at": now - timedelta(days=2)},
        {"created_at": now - timedelta(days=2, hours=1)},
        {"created_at": now - timedelta(days=2, hours=2)},
        {"created_at": now - timedelta(days=6)},  # 早于 lookback 窗口
        {"created_at": now},  # 仍在热窗口
    ])

    service, store = _archive_service(tmp_path, config=_config(hot_retention_days=1, chunk_rows=2))
    batch = service.trigger(tenant_id=tenant_id, actor_user_id=user_id, lookback_hours=48, trigger_source="MANUAL")

    assert batch["status"] == "SUCCEEDED", batch
    assert batch["rows"] == 3, "只归档 [now-72h, now-24h) 区间"
    assert batch["encryption"] == "none"
    assert len(batch["objects"]) == 2

    prefix = batch["objects"][0]["object_key"].rsplit("/part-", 1)[0]
    manifest = json.loads(_store_bytes(store, f"{prefix}/manifest.json").decode("utf-8"))
    assert manifest["row_count"] == 3
    assert manifest["sha256_total"] == batch["sha256_total"]

    digests = []
    for item in batch["objects"]:
        body = _store_bytes(store, item["object_key"])
        digests.append(hashlib.sha256(body).hexdigest())
        assert item["sha256"] == digests[-1]
    assert sha256_total(digests) == batch["sha256_total"]

    with SessionLocal() as db:
        assert db.get(AuditArchiveBatch, UUID(batch["id"])) is not None


def test_encrypted_archive_roundtrip_and_tamper_detection(tmp_path):
    tenant_id, user_id = _tenant_and_user()
    _seed_audit(tenant_id, [{"created_at": utc_now() - timedelta(days=2), "action": "AGENT_CREATED"}])

    provider = FakeSecretProvider({"archive-ref": _ARCHIVE_KEY})
    service, store = _archive_service(
        tmp_path,
        config=_config(
            hot_retention_days=1,
            chunk_rows=5,
            archive_encryption_enabled=True,
            archive_secret_ref="secret://archive-ref",
        ),
        secret=provider,
    )
    batch = service.trigger(tenant_id=tenant_id, actor_user_id=user_id, lookback_hours=48)

    assert batch["status"] == "SUCCEEDED", batch
    assert batch["encryption"] == "aes-256-gcm"
    object_key = batch["objects"][0]["object_key"]
    assert object_key.endswith(".jsonl.enc")

    key = _derive_key(_ARCHIVE_KEY)
    ciphertext = _store_bytes(store, object_key)
    assert ciphertext[:4] == b"ZAE1", "密文必须自描述，离线工具才能识别格式"
    plaintext = decrypt_archive_payload(ciphertext, key)
    assert hashlib.sha256(plaintext).hexdigest() == batch["objects"][0]["sha256"], "sha256 必须针对明文计算"
    assert json.loads(plaintext.splitlines()[0])["action"] == "AGENT_CREATED"

    # 密钥错误 / 密文被篡改都必须失败（GCM tag 校验），不得静默返回脏数据
    with pytest.raises(Exception):
        decrypt_archive_payload(ciphertext, _derive_key("wrong-key"))
    tampered = bytearray(ciphertext)
    tampered[-1] ^= 0xFF
    with pytest.raises(Exception):
        decrypt_archive_payload(bytes(tampered), key)
    with pytest.raises(ValueError):
        decrypt_archive_payload(b"plain-jsonl-bytes", key)


def test_archive_key_failure_happens_before_batch_is_created(tmp_path):
    tenant_id, user_id = _tenant_and_user()
    _seed_audit(tenant_id, [{"created_at": utc_now() - timedelta(days=2)}])

    # 场景一：声明了密钥引用但没有 secret.provider Port
    no_provider, _ = _archive_service(
        tmp_path / "a",
        config=_config(archive_encryption_enabled=True, archive_secret_ref="secret://missing"),
    )
    with pytest.raises(ApiError) as excinfo:
        no_provider.trigger(tenant_id=tenant_id, actor_user_id=user_id)
    assert excinfo.value.code == "AI_CAPABILITY_UNAVAILABLE"

    # 场景二：Port 在但解密失败（密钥轮换/误配）→ 502，且同样不留空批次
    bad_secret, _ = _archive_service(
        tmp_path / "b",
        config=_config(archive_encryption_enabled=True, archive_secret_ref="secret://wrong"),
        secret=FakeSecretProvider({}),
    )
    with pytest.raises(ApiError) as excinfo2:
        bad_secret.trigger(tenant_id=tenant_id, actor_user_id=user_id)
    assert excinfo2.value.code == "AI_AUDIT_ARCHIVE_FAILED"

    with SessionLocal() as db:
        batches = db.scalars(select(AuditArchiveBatch).where(AuditArchiveBatch.tenant_id == tenant_id)).all()
        assert batches == [], "密钥问题必须在创建批次之前失败，不能留下永远 FAILED 的空批次"


def test_purge_expired_deletes_objects_but_keeps_job_row(tmp_path):
    tenant_id, user_id = _tenant_and_user()
    _seed_audit(tenant_id, [{"action": "AGENT_CREATED"}])

    service, store = _service(tmp_path, config=_config(chunk_rows=10))
    try:
        payload = _wait_job(service, tenant_id, service.create_job(
            tenant_id=tenant_id, actor_user_id=user_id, filters={}, export_format="jsonl"
        )["id"])
        prefix = _job_row(payload["id"]).object_prefix
        assert store.local_path(f"{prefix}/manifest.json").exists()

        with SessionLocal.begin() as db:
            job = db.get(AuditExportJob, payload["id"])
            job.expires_at = utc_now() - timedelta(seconds=1)

        assert service.purge_expired() == 1
        assert not store.local_path(f"{prefix}/manifest.json").exists()
        assert not store.local_path(f"{prefix}/part-00001.jsonl").exists()

        with SessionLocal() as db:
            assert db.get(AuditExportJob, payload["id"]) is not None, "任务行是审计追溯凭据，不得删除"
            assert db.scalars(select(AuditExportShard).where(AuditExportShard.job_id == payload["id"])).all() == []
    finally:
        service.shutdown()


def test_encrypt_decrypt_helpers_reject_short_or_foreign_payloads():
    key = _derive_key(_ARCHIVE_KEY)
    payload = encrypt_archive_payload(b'{"a":1}\n', key)

    assert decrypt_archive_payload(payload, key) == b'{"a":1}\n'
    for bad in (b"", b"ZAE1", b"ZAE1" + b"\x00" * 5, b"XXXX" + b"\x00" * 32):
        with pytest.raises(ValueError):
            decrypt_archive_payload(bad, key)


# ---------------------------------------------------------------- 观测接线（observability.sink）


# payload 键名里出现这些片段即视为泄漏：租户、文件名、审计正文、脚本内容一律不得上报
_SENSITIVE_KEY_HINTS = (
    "token",
    "secret",
    "file",
    "body",
    "content",
    "script",
    "tenant",
    "object_key",
    "prefix",
    "query",
    "sql",
)


def _capture_events(monkeypatch) -> list[tuple[str, dict]]:
    """把 `jobs.emit_event` 换成本地记录器（真实 sink 不参与，避免污染其它用例）。"""
    events: list[tuple[str, dict]] = []

    def _fake_emit(event: str, payload: dict) -> None:
        events.append((event, dict(payload)))

    monkeypatch.setattr(audit_jobs, "emit_event", _fake_emit)
    return events


def _wait_event(events: list[tuple[str, dict]], status: str, *, timeout: float = 15.0) -> dict:
    """等某个状态的事件出现（终态在 DB 已提交之后才上报，所以要轮询而不是假设同步）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for name, payload in list(events):
            if name == AUDIT_EXPORT_EVENT and payload.get("status") == status:
                return payload
        time.sleep(0.01)
    raise AssertionError(f"event {AUDIT_EXPORT_EVENT}/{status} not emitted: {events}")


def _assert_payload_whitelist(payload: dict, expected: tuple[str, ...]) -> None:
    """键集合精确匹配白名单，且不含任何敏感键名。"""
    assert set(payload) == set(expected), f"payload keys drifted: {sorted(payload)} != {sorted(expected)}"
    for key in payload:
        lowered = key.lower()
        assert not any(hint in lowered for hint in _SENSITIVE_KEY_HINTS), f"sensitive key leaked: {key}"


def test_lifecycle_events_carry_exact_payload_fields(tmp_path, monkeypatch):
    """成功路径：queued → running → succeeded，三者键集合都精确等于白名单。"""
    tenant_id, user_id = _tenant_and_user()
    _seed_audit(tenant_id, [{"action": "AGENT_CREATED"} for _ in range(5)])
    events = _capture_events(monkeypatch)

    service, _store = _service(tmp_path)
    try:
        created = service.create_job(
            tenant_id=tenant_id, actor_user_id=user_id, filters={}, export_format="jsonl"
        )
        payload = _wait_job(service, tenant_id, created["id"])
        succeeded = _wait_event(events, "succeeded")
    finally:
        service.shutdown()

    assert payload["status"] == "SUCCEEDED", payload
    assert [name for name, _ in events] == [AUDIT_EXPORT_EVENT] * 3, events
    assert [item["status"] for _, item in events] == ["queued", "running", "succeeded"], events

    for _, item in events:
        _assert_payload_whitelist(item, AUDIT_EXPORT_EVENT_FIELDS)

    assert succeeded["job_id"] == str(created["id"])
    assert succeeded["format"] == "jsonl"
    assert succeeded["rows_written"] == 5
    assert succeeded["chunk_count"] == 3, "chunk_rows=2 + 5 行 → 3 片"
    assert succeeded["duration_ms"] >= 0

    # 租户 id 明文绝不出现（不是"脱敏"，而是根本不采集）
    dumped = json.dumps(events, default=str)
    assert str(tenant_id) not in dumped
    assert str(user_id) not in dumped


def test_failed_job_event_adds_only_error_code(tmp_path, monkeypatch):
    """失败路径：在基础白名单之上只多一个 error_code，其余键不变。"""
    tenant_id, user_id = _tenant_and_user()
    _seed_audit(tenant_id, [{"action": "AGENT_CREATED"} for _ in range(5)])
    events = _capture_events(monkeypatch)

    service, _store = _service(tmp_path, config=_config(max_rows=2, chunk_rows=2))
    try:
        created = service.create_job(
            tenant_id=tenant_id, actor_user_id=user_id, filters={}, export_format="jsonl"
        )
        payload = _wait_job(service, tenant_id, created["id"])
        failed = _wait_event(events, "failed")
    finally:
        service.shutdown()

    assert payload["status"] == "FAILED", payload
    _assert_payload_whitelist(failed, AUDIT_EXPORT_EVENT_FAILURE_FIELDS)
    assert failed["error_code"] == "AI_AUDIT_EXPORT_TOO_LARGE"
    assert failed["rows_written"] == 0 and failed["chunk_count"] == 0, "失败任务不得上报'看起来成功'的行数"
    assert failed["job_id"] == str(created["id"])
    assert str(tenant_id) not in json.dumps(failed, default=str)


def test_sink_failure_never_breaks_export(tmp_path, monkeypatch):
    """sink 抛错只记日志：导出照常成功，业务返回值不受影响。"""

    def _boom(event: str, payload: dict) -> None:
        raise RuntimeError("sink down")

    monkeypatch.setattr(audit_jobs, "emit_event", _boom)

    tenant_id, user_id = _tenant_and_user()
    _seed_audit(tenant_id, [{"action": "AGENT_CREATED"} for _ in range(5)])

    service, store = _service(tmp_path)
    try:
        created = service.create_job(
            tenant_id=tenant_id, actor_user_id=user_id, filters={}, export_format="jsonl"
        )
        payload = _wait_job(service, tenant_id, created["id"])
    finally:
        service.shutdown()

    assert payload["status"] == "SUCCEEDED", payload
    assert payload["rows_total"] == 5
    prefix = _job_row(created["id"]).object_prefix
    assert store.local_path(f"{prefix}/manifest.json").exists(), "观测失败不得导致产物缺失"


# ---------------------------------------------------------------- 多副本租约（v1.3 §5.7）


def _insert_pending_job(tenant_id, user_id) -> UUID:
    """直接落一条 PENDING 任务行：绕开 create_job 的自动入队，便于手工仲裁租约。"""
    with SessionLocal.begin() as db:
        job = AuditExportJob(
            tenant_id=tenant_id,
            created_by=user_id,
            status="PENDING",
            export_format="jsonl",
            query_json={},
            expires_at=utc_now() + timedelta(days=1),
        )
        db.add(job)
        db.flush()
        return job.id


def _lease_of(job_id) -> tuple[str | None, object, int]:
    row = _job_row(job_id)
    return row.lease_owner, row.lease_expires_at, row.attempts


def test_claim_is_exclusive_between_replicas(tmp_path):
    """同一个 PENDING 任务只能被一个副本抢到：仲裁点是条件 UPDATE 的 rowcount。"""
    tenant_id, user_id = _tenant_and_user()
    replica_a, _ = _service(tmp_path, config=_config(instance_id="replica-a"))
    replica_b, _ = _service(tmp_path, config=_config(instance_id="replica-b"))
    job_id = _insert_pending_job(tenant_id, user_id)
    try:
        assert replica_a._claim(job_id) is True
        assert replica_b._claim(job_id) is False, "任务已是 RUNNING 且租约有效，第二个副本不得抢到"

        owner, expires_at, attempts = _lease_of(job_id)
        assert owner == "replica-a"
        assert expires_at is not None
        assert attempts == 1
        assert _job_row(job_id).status == "RUNNING"
    finally:
        replica_a.shutdown()
        replica_b.shutdown()


def test_recover_interrupted_spares_peer_lease(tmp_path):
    """重启恢复不得误杀其他副本的在途任务——这正是引入租约要解决的问题。"""
    tenant_id, user_id = _tenant_and_user()
    peer, _ = _service(tmp_path, config=_config(instance_id="peer"))
    job_id = _insert_pending_job(tenant_id, user_id)
    try:
        assert peer._claim(job_id) is True

        # 新副本启动：它自己的 recover 只能看到 peer 的有效租约，必须一条都不回收
        newcomer, _ = _service(tmp_path, config=_config(instance_id="newcomer"))

        assert newcomer.recover_interrupted() == 0, "有效租约内的 RUNNING 任务不是遗留任务"
        assert _job_row(job_id).status == "RUNNING"

        # 租约过期 = 持有者失联，此时才允许回收
        with SessionLocal.begin() as db:
            db.get(AuditExportJob, job_id).lease_expires_at = utc_now() - timedelta(seconds=1)
        assert newcomer.recover_interrupted() == 1

        row = _job_row(job_id)
        assert row.status == "FAILED"
        assert row.error_code == "AI_AUDIT_EXPORT_FAILED"
        assert row.lease_owner is None and row.lease_expires_at is None, "回收后必须释放租约"
        newcomer.shutdown()
    finally:
        peer.shutdown()


def test_lease_renew_extends_expiry_and_detects_loss(tmp_path):
    """续租把到期时间推后；一旦 owner 变了（被接管），续租必须报错让执行方停手。"""
    tenant_id, user_id = _tenant_and_user()
    replica, _ = _service(tmp_path, config=_config(instance_id="replica-a"))
    job_id = _insert_pending_job(tenant_id, user_id)
    try:
        assert replica._claim(job_id) is True
        first_expiry = _job_row(job_id).lease_expires_at

        replica._renew(job_id)
        second_expiry = _job_row(job_id).lease_expires_at
        assert first_expiry is not None and second_expiry is not None
        assert second_expiry >= first_expiry

        with SessionLocal.begin() as db:
            db.get(AuditExportJob, job_id).lease_owner = "intruder"
        with pytest.raises(ApiError) as excinfo:
            replica._renew(job_id)
        assert excinfo.value.code == "AI_AUDIT_EXPORT_FAILED"
    finally:
        replica.shutdown()


def test_failed_path_does_not_touch_peer_owned_job(tmp_path):
    """已丢租的副本不得把对方的任务标失败，也不得删对方正在写的分片。"""
    tenant_id, user_id = _tenant_and_user()
    replica, _ = _service(tmp_path, config=_config(instance_id="replica-a"))
    job_id = _insert_pending_job(tenant_id, user_id)
    try:
        assert replica._claim(job_id) is True
        with SessionLocal.begin() as db:
            db.get(AuditExportJob, job_id).lease_owner = "replica-b"

        replica._mark_failed(job_id, "AI_AUDIT_EXPORT_FAILED", "boom")

        row = _job_row(job_id)
        assert row.status == "RUNNING", "丢租后仍把状态改成 FAILED 就是双写"
        assert row.lease_owner == "replica-b"
        assert row.error_code is None
    finally:
        replica.shutdown()


def test_lease_is_released_when_job_finishes(tmp_path):
    """终态释放租约：否则运维无法从表里判断"还有没有实例在跑"。"""
    tenant_id, user_id = _tenant_and_user()
    _seed_audit(tenant_id, [{"action": "AGENT_CREATED"}])

    service, _store = _service(tmp_path, config=_config(chunk_rows=10, instance_id="replica-a"))
    try:
        done = _wait_job(service, tenant_id, service.create_job(
            tenant_id=tenant_id, actor_user_id=user_id, filters={}, export_format="jsonl"
        )["id"])
    finally:
        service.shutdown()

    assert done["status"] == "SUCCEEDED"
    owner, expires_at, attempts = _lease_of(done["id"])
    assert owner is None and expires_at is None
    assert attempts == 1, "一次成功执行只应抢一次租约"
