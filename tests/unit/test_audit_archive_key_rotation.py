"""归档冷副本密钥轮换专项单测（v1.3 §12.9）。

四条红线（对应验收口径）：
1. 双密钥并存：当前密钥 + 上一把密钥都能解密，写入恒用当前密钥；
2. 切换可见：轮换后新产物的 manifest / 索引记录新 `key_id`，旧对象仍可读；
3. 缺失必报：旧密钥被 retire 后仍遇到只有它能解的对象 → `AI_AUDIT_ARCHIVE_KEY_MISSING`，
   不静默跳过、不返回脏数据；
4. 闸门可靠：`check_retire` 只在"每个加密对象都能只用当前密钥解开"时放行。

用例跑真实 `AuditArchiveService` + 真实 `LocalObjectStore` + 真实 `SessionLocal`，
只有 Port 注册表、SDK 与 secret.provider 被替身化。
"""

from __future__ import annotations

import io
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.builtin_plugins.audit_export_archive.archiver import AuditArchiveService
from app.builtin_plugins.audit_export_archive.config import AuditExportConfig
from app.builtin_plugins.audit_export_archive.keys import (
    KEY_MISSING,
    KEY_SOURCE_CURRENT,
    KEY_SOURCE_PREVIOUS,
    ArchiveKey,
    ArchiveKeyRing,
    build_key_ring,
    decrypt_archive_payload,
    encrypt_archive_payload,
    key_id_of,
)
from app.builtin_plugins.object_store import LocalObjectStore
from app.core.errors import PLUGIN_ERROR_CODES, PLUGIN_ERROR_STATUS, ApiError
from app.core.ids import utc_now
from app.db.session import SessionLocal
from app.models import AuditLog, Tenant, User
from scripts.rotate_audit_archive_key import (
    REASON_PREVIOUS_ONLY,
    ArchiveObjectRef,
    check_retire,
    collect_archive_objects,
    register_new_secret,
    summarize,
)
from tests.support.fakes import FakeRegistry, FakeSdk, FakeSecretProvider

_OLD_SECRET = "archive-key-2026-h1"
_NEW_SECRET = "archive-key-2026-h2"


def _key(secret: str, *, source: str = KEY_SOURCE_CURRENT) -> ArchiveKey:
    return ArchiveKey.from_secret(secret, source=source)


def _ring(*, previous: str | None = None) -> ArchiveKeyRing:
    return ArchiveKeyRing(
        current=_key(_NEW_SECRET),
        previous=_key(previous, source=KEY_SOURCE_PREVIOUS) if previous else None,
    )


def _config(**overrides) -> AuditExportConfig:
    base = dict(
        max_rows=500,
        chunk_rows=5,
        max_concurrency=2,
        retry_limit=0,
        hot_retention_days=1,
        archive_encryption_enabled=False,
        archive_schedule_hours=24.0,
        export_formats=("jsonl", "csv"),
    )
    base.update(overrides)
    return AuditExportConfig(**base)


def _tenant_and_user():
    tenant_id = uuid4()
    with SessionLocal.begin() as db:
        db.add(Tenant(id=tenant_id, name=f"t-{tenant_id}"))
        db.flush()
        user = User(tenant_id=tenant_id, username=f"u-{uuid4()}", password_hash="x")
        db.add(user)
        db.flush()
        user_id = user.id
    return tenant_id, user_id


def _seed_audit(tenant_id, rows: int = 3) -> None:
    with SessionLocal.begin() as db:
        for index in range(rows):
            db.add(
                AuditLog(
                    tenant_id=tenant_id,
                    action="AGENT_CREATED",
                    resource_type="AGENT",
                    summary=f"s-{index}",
                    created_at=utc_now() - timedelta(days=2),
                )
            )


def _archive_service(tmp_path, *, config: AuditExportConfig, secret):
    store = LocalObjectStore(tmp_path / "objects")
    sdk = FakeSdk(settings=SimpleNamespace(), registry=FakeRegistry({"object.store": store, "secret.provider": secret}))
    return AuditArchiveService(sdk, config, guard=None), store


# ---------------------------------------------------------------- 密钥环


def test_write_always_uses_current_key_while_previous_stays_readable() -> None:
    old_payload = encrypt_archive_payload(b'{"row": 1}\n', _key(_OLD_SECRET).key)

    ring = _ring(previous=_OLD_SECRET)
    plaintext, used = ring.decrypt(old_payload)

    assert plaintext == b'{"row": 1}\n'
    assert used.source == KEY_SOURCE_PREVIOUS, "历史对象必须由上一把密钥读出"
    # 写入恒用当前密钥：上一把密钥只读
    assert ring.write_key == _key(_NEW_SECRET).key
    assert ring.write_key_id == key_id_of(_key(_NEW_SECRET).key)
    assert decrypt_archive_payload(encrypt_archive_payload(b"x", ring.write_key), _key(_NEW_SECRET).key) == b"x"
    with pytest.raises(Exception):
        decrypt_archive_payload(old_payload, ring.write_key)


def test_retired_key_raises_key_missing_instead_of_skipping() -> None:
    old_payload = encrypt_archive_payload(b"secret-row\n", _key(_OLD_SECRET).key)
    retired = _ring()  # 只留当前密钥：旧密钥已 retire

    with pytest.raises(ApiError) as excinfo:
        retired.decrypt(old_payload)
    assert excinfo.value.code == KEY_MISSING
    assert excinfo.value.status_code == 502

    with pytest.raises(ApiError) as excinfo:
        retired.decrypt_with_current(old_payload)
    assert excinfo.value.code == KEY_MISSING

    # 错误码必须进注册表与状态映射，否则 API 层会兜底成 AI_INTERNAL
    assert KEY_MISSING in PLUGIN_ERROR_CODES
    assert PLUGIN_ERROR_STATUS[KEY_MISSING] == 502


def test_build_key_ring_rejects_mismatched_or_broken_refs() -> None:
    provider = FakeSecretProvider({"cur": _NEW_SECRET, "prev": _OLD_SECRET, "same": _NEW_SECRET})

    ring = build_key_ring(enabled=True, secret_ref="secret://cur", previous_secret_ref="secret://prev", provider=provider)
    assert ring is not None
    assert ring.write_key_id == key_id_of(_key(_NEW_SECRET).key)
    assert ring.previous is not None and ring.previous.key_id == key_id_of(_key(_OLD_SECRET).key)

    # 同一引用既当写密钥又当只读密钥 = 没轮换
    with pytest.raises(ApiError) as excinfo:
        build_key_ring(enabled=True, secret_ref="secret://cur", previous_secret_ref="secret://cur", provider=provider)
    assert excinfo.value.code == "AI_AUDIT_ARCHIVE_FAILED"

    # 不同引用解出同一把密钥：塌缩成一把，不报错（否则轮换脚本会误报）
    collapsed = build_key_ring(enabled=True, secret_ref="secret://cur", previous_secret_ref="secret://same", provider=provider)
    assert collapsed is not None and collapsed.previous is None

    # 上一把密钥解不开（KMS 里已删）必须当场失败，不能静默降级成"只有当前密钥"
    with pytest.raises(ApiError) as excinfo:
        build_key_ring(enabled=True, secret_ref="secret://cur", previous_secret_ref="secret://gone", provider=provider)
    assert excinfo.value.code == "AI_AUDIT_ARCHIVE_FAILED"

    assert build_key_ring(enabled=False, secret_ref="secret://cur", provider=provider) is None


def test_config_rejects_previous_key_misuse() -> None:
    # 上一把密钥只能在加密开启时存在
    with pytest.raises(ValueError):
        _config(archive_previous_secret_ref="secret://prev").validate()
    # 同一引用既当当前密钥又当上一把 = 没轮换
    with pytest.raises(ValueError):
        _config(
            archive_encryption_enabled=True,
            archive_secret_ref="secret://cur",
            archive_previous_secret_ref="secret://cur",
        ).validate()
    ok = _config(
        archive_encryption_enabled=True,
        archive_secret_ref="secret://cur",
        archive_previous_secret_ref="secret://prev",
    )
    ok.validate()
    assert ok.archive_previous_secret_ref == "secret://prev"


# ---------------------------------------------------------------- 归档写入 + 轮换


def test_archive_records_key_id_and_rotation_keeps_old_objects_readable(tmp_path) -> None:
    tenant_id, user_id = _tenant_and_user()
    _seed_audit(tenant_id)
    provider = FakeSecretProvider({"old-ref": _OLD_SECRET, "new-ref": _NEW_SECRET})

    # 阶段一：轮换前，当前密钥 = old
    before, store = _archive_service(
        tmp_path,
        config=_config(archive_encryption_enabled=True, archive_secret_ref="secret://old-ref"),
        secret=provider,
    )
    first = before.trigger(tenant_id=tenant_id, actor_user_id=user_id, lookback_hours=48)
    assert first["status"] == "SUCCEEDED", first
    old_key_id = key_id_of(_key(_OLD_SECRET).key)
    assert {item["key_id"] for item in first["objects"]} == {old_key_id}, "索引必须记录密钥归属"

    prefix = first["objects"][0]["object_key"].rsplit("/part-", 1)[0]
    manifest_bytes = store.local_path(f"{prefix}/manifest.json.enc").read_bytes()
    manifest = json.loads(decrypt_archive_payload(manifest_bytes, _key(_OLD_SECRET).key))
    assert manifest["key_id"] == old_key_id, "manifest 记录 key_id，且不落密钥明文"
    assert _OLD_SECRET not in json.dumps(manifest)

    # 阶段二：轮换后，写密钥 = new，old 只读
    after, _ = _archive_service(
        tmp_path,
        config=_config(
            archive_encryption_enabled=True,
            archive_secret_ref="secret://new-ref",
            archive_previous_secret_ref="secret://old-ref",
        ),
        secret=provider,
    )
    second = after.trigger(tenant_id=tenant_id, actor_user_id=user_id, lookback_hours=48)
    assert second["status"] == "SUCCEEDED", second
    new_key_id = key_id_of(_key(_NEW_SECRET).key)
    assert {item["key_id"] for item in second["objects"]} == {new_key_id}, "新写入必须用当前密钥"

    ring = _ring(previous=_OLD_SECRET)
    old_payload = store.local_path(first["objects"][0]["object_key"]).read_bytes()
    plaintext, used = ring.decrypt(old_payload)
    assert plaintext and used.source == KEY_SOURCE_PREVIOUS, "轮换后旧对象仍然可读"

    # 索引视角与真解密视角一致：旧批次阻塞 retire，新批次不阻塞
    refs = collect_archive_objects(SessionLocal, tenant_id=tenant_id)
    summary = summarize(refs)
    assert summary.by_key_id[old_key_id] >= 2  # 分片 + manifest
    assert summary.by_key_id[new_key_id] >= 2
    assert summary.index_safe_to_retire(new_key_id) is False

    old_refs = [ref for ref in refs if ref.key_id == old_key_id and ref.encrypted]
    old_check = check_retire(old_refs, store, ring)
    assert old_check.safe is False
    assert {blocker.reason for blocker in old_check.blockers} == {REASON_PREVIOUS_ONLY}

    new_refs = [ref for ref in refs if ref.key_id == new_key_id and ref.encrypted]
    new_check = check_retire(new_refs, store, ring)
    assert new_check.safe is True
    assert new_check.readable_with_current == len(new_refs)

    # retire 之后（只剩当前密钥）再读旧对象：必须报 KEY_MISSING，不得静默跳过
    with pytest.raises(ApiError) as excinfo:
        _ring().decrypt(old_payload)
    assert excinfo.value.code == KEY_MISSING


def test_check_retire_reports_missing_and_index_mismatch(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    key = _key(_NEW_SECRET)
    present_key = "audit-archive/t/1/part-00001.jsonl.enc"
    store.put(present_key, io.BytesIO(encrypt_archive_payload(b"row\n", key.key)), max_bytes=1024)

    refs = [
        # 对象缺失：属于悬空引用（§12.8），本身已不可解，不构成 retire 阻塞
        ArchiveObjectRef(batch_id="b1", object_key="audit-archive/t/1/part-00002.jsonl.enc", key_id=key.key_id, encrypted=True),
        # 索引声明旧密钥、实际由当前密钥写入：只告警，不阻塞
        ArchiveObjectRef(batch_id="b1", object_key=present_key, key_id=_key(_OLD_SECRET).key_id, encrypted=True),
        # 明文对象不参与密钥判定
        ArchiveObjectRef(batch_id="b1", object_key="audit-archive/t/1/manifest.json", key_id=None, encrypted=False),
    ]
    ring = _ring()
    check = check_retire(refs, store, ring)

    assert check.safe is True
    assert len(check.missing) == 1
    assert check.plaintext_skipped == 1
    assert check.readable_with_current == 1
    assert len(check.warnings) == 1 and "stale" in check.warnings[0].reason


def test_register_new_secret_roundtrip_and_rejects_empty() -> None:
    provider = FakeSecretProvider()
    ref = register_new_secret(provider, _NEW_SECRET)

    assert ref.startswith("secret://")
    assert _NEW_SECRET not in ref, "落配置的必须只是密文引用"
    assert provider.decrypt(ref.split("://", 1)[1]) == _NEW_SECRET

    with pytest.raises(ValueError):
        register_new_secret(provider, "   ")
