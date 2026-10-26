"""归档冷副本密钥轮换（v1.3 §12.9）：登记新密钥 → 切换写入 → 旧密钥只读 → 安全 retire。

设计前提
--------
归档密文**不内嵌密钥标识**（密文不该携带密钥线索），"这批对象是谁加密的"只能看索引
（`zr_audit_archive_batch.objects[].key_id`）与 manifest 的 `key_id`；"能不能解开"只能真解。
所以轮换分三档，永不出现"中间态读不出"：

1. `--new-secret-file` / `--new-secret-stdin`：用 `secret.provider` 加密新密钥材料，
   产出 `secret://...` 引用并**回读自检**；脚本不写配置文件，只打印要粘贴的两行 env
   （`AUDIT_ARCHIVE_SECRET_REF` = 新，`AUDIT_ARCHIVE_PREVIOUS_SECRET_REF` = 旧）。
2. 切换后（默认 `--status`）：只读索引统计对象按 `key_id` 的分布，给出"索引视角能否 retire"。
   零写入、不碰对象存储。
3. `--retire-old`：真读对象存储 + 真解密。判据是"**每个**加密对象都能只用当前密钥解开"：
   - 有一个只能用旧密钥解 → 拒绝 retire，报 `AI_AUDIT_ARCHIVE_KEY_MISSING`，退出码 3；
   - 当前 + 旧密钥都解不开（数据损坏 / 密钥丢失）→ 同样拒绝，报同一错误码；
   - 对象缺失 → 单列统计（那是 §12.8 的悬空引用，本身已不可解，不构成 retire 阻塞）；
   - 全部通过 → 打印可移除的 env 行（`AUDIT_ARCHIVE_PREVIOUS_SECRET_REF=`），退出码 0。

脚本**不**改 `.env`、**不**改数据库：密钥归属是运维态配置，由人工按打印结果落地。
退出码：0 = 通过/安全；3 = 被旧密钥依赖阻塞；2 = 运行失败（配置/存储/数据库）。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 插件私有表（方案 b）：模型随插件目录，必须显式 import，否则 ORM 映射不在 Base.metadata 里
import app.builtin_plugins.audit_export_archive.models  # noqa: E402, F401
from app.builtin_plugins.audit_export_archive.config import AuditExportConfig  # noqa: E402
from app.builtin_plugins.audit_export_archive.keys import (  # noqa: E402
    KEY_MISSING,
    KEY_SOURCE_PREVIOUS,
    SECRET_PREFIX,
    ArchiveKeyRing,
    build_key_ring,
    derive_key,
    is_encrypted_payload,
    key_id_of,
)
from app.builtin_plugins.audit_export_archive.models import (  # noqa: E402
    STATUS_SUCCEEDED,
    AuditArchiveBatch,
)

_ENCRYPTION_AES_GCM = "aes-256-gcm"
_LEGACY_KEY_ID = "legacy(no-key-id)"

REASON_PREVIOUS_ONLY = "decrypts with the previous key only"
REASON_UNREADABLE = "not decryptable with any configured key"
REASON_NOT_ENCRYPTED = "index says encrypted but the object is plaintext"


@dataclass(frozen=True)
class ArchiveObjectRef:
    """索引里一条"归档对象"引用（含 manifest），带索引侧声明的密钥归属。"""

    batch_id: str
    object_key: str
    key_id: str | None
    encrypted: bool

    @property
    def indexed_key_id(self) -> str:
        return self.key_id or _LEGACY_KEY_ID


@dataclass
class UsageSummary:
    total: int = 0
    encrypted: int = 0
    plaintext: int = 0
    by_key_id: dict[str, int] = field(default_factory=dict)

    def add(self, ref: ArchiveObjectRef) -> None:
        self.total += 1
        if not ref.encrypted:
            self.plaintext += 1
            return
        self.encrypted += 1
        self.by_key_id[ref.indexed_key_id] = self.by_key_id.get(ref.indexed_key_id, 0) + 1

    def index_safe_to_retire(self, current_key_id: str) -> bool:
        """索引视角：所有加密对象都声称由当前密钥写入，才可能安全 retire。"""
        return all(key == current_key_id for key in self.by_key_id)


@dataclass(frozen=True)
class RetireBlocker:
    object_key: str
    batch_id: str
    reason: str


@dataclass
class RetireCheck:
    checked: int = 0
    readable_with_current: int = 0
    plaintext_skipped: int = 0
    missing: list[ArchiveObjectRef] = field(default_factory=list)
    blockers: list[RetireBlocker] = field(default_factory=list)
    # 不阻塞 retire，但必须报出来的索引/对象不一致（索引滞后、声明与事实不符）
    warnings: list[RetireBlocker] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        return not self.blockers


def collect_archive_objects(
    session_factory: Callable[[], Session],
    *,
    tenant_id: UUID | None = None,
) -> list[ArchiveObjectRef]:
    """索引侧枚举：批次行 + `objects` 清单 + 成功批次的 manifest 键。不扫存储。"""
    refs: list[ArchiveObjectRef] = []
    with session_factory() as session:
        stmt = select(AuditArchiveBatch)
        if tenant_id is not None:
            stmt = stmt.where(AuditArchiveBatch.tenant_id == tenant_id)
        for batch in session.scalars(stmt).all():
            encrypted = batch.encryption == _ENCRYPTION_AES_GCM
            for item in batch.objects or []:
                record = item if isinstance(item, dict) else {}
                key = str(record.get("object_key") or "")
                if not key:
                    continue
                declared = record.get("key_id")
                refs.append(
                    ArchiveObjectRef(
                        batch_id=str(batch.id),
                        object_key=key,
                        key_id=str(declared) if declared else None,
                        encrypted=encrypted,
                    )
                )
            if batch.status == STATUS_SUCCEEDED and batch.object_prefix:
                leaf = "manifest.json.enc" if encrypted else "manifest.json"
                refs.append(
                    ArchiveObjectRef(
                        batch_id=str(batch.id),
                        object_key=f"{batch.object_prefix}/{leaf}",
                        # manifest 是整批一个 key，索引里取第一条对象的声明即可
                        key_id=_manifest_key_id(batch.objects or []),
                        encrypted=encrypted,
                    )
                )
    return refs


def _manifest_key_id(objects: Iterable[Any]) -> str | None:
    for item in objects:
        if isinstance(item, dict) and item.get("key_id"):
            return str(item["key_id"])
    return None


def summarize(refs: Iterable[ArchiveObjectRef]) -> UsageSummary:
    summary = UsageSummary()
    for ref in refs:
        summary.add(ref)
    return summary


def _read_payload(store: Any, object_key: str) -> bytes | None:
    path = Path(store.local_path(object_key))
    if not path.exists():
        return None
    return path.read_bytes()


def check_retire(refs: Iterable[ArchiveObjectRef], store: Any, ring: ArchiveKeyRing) -> RetireCheck:
    """真解密校验：每个加密对象都必须能只用**当前**密钥解开。"""
    result = RetireCheck()
    for ref in refs:
        if not ref.encrypted:
            result.plaintext_skipped += 1
            continue
        payload = _read_payload(store, ref.object_key)
        if payload is None:
            result.missing.append(ref)
            continue
        if not is_encrypted_payload(payload):
            # 索引说加密、对象却是明文：密钥轮换救不了它，按不可读上报（绝不静默跳过）
            result.blockers.append(RetireBlocker(ref.object_key, ref.batch_id, REASON_NOT_ENCRYPTED))
            continue
        result.checked += 1
        try:
            ring.decrypt_with_current(payload)
        except Exception:  # noqa: BLE001 - 当前密钥解不开：先判断是"只差旧密钥"还是"彻底解不开"
            try:
                _plaintext, used = ring.decrypt(payload)
            except Exception:  # noqa: BLE001 - 全环都失败 = 密钥丢失/对象损坏
                result.blockers.append(RetireBlocker(ref.object_key, ref.batch_id, REASON_UNREADABLE))
                continue
            reason = REASON_PREVIOUS_ONLY if used.source == KEY_SOURCE_PREVIOUS else REASON_UNREADABLE
            result.blockers.append(RetireBlocker(ref.object_key, ref.batch_id, reason))
            continue
        if ref.key_id is not None and ref.key_id != ring.write_key_id:
            # 索引声明旧密钥、实际当前密钥能解：索引滞后，不阻塞 retire，但要报出来
            result.warnings.append(RetireBlocker(ref.object_key, ref.batch_id, "index key_id is stale but object uses the current key"))
            result.readable_with_current += 1
            continue
        result.readable_with_current += 1
    return result


def register_new_secret(provider: Any, plaintext: str) -> str:
    """把新密钥材料交给 secret.provider 加密，返回 `secret://` 引用（并回读自检）。"""
    if not plaintext.strip():
        raise ValueError("new archive key material is empty")
    ciphertext = provider.encrypt(plaintext)
    if provider.decrypt(ciphertext) != plaintext:
        raise ValueError("secret provider round-trip check failed")
    return f"{SECRET_PREFIX}{ciphertext}"


def _load_new_secret(args: argparse.Namespace) -> str:
    if args.new_secret_file:
        return Path(args.new_secret_file).read_text(encoding="utf-8").rstrip("\r\n")
    # stdin：避免明文进 shell 历史 / 进程列表
    return sys.stdin.readline().rstrip("\r\n")


def _print_usage(summary: UsageSummary, ring: ArchiveKeyRing) -> None:
    print(f"objects total={summary.total} encrypted={summary.encrypted} plaintext={summary.plaintext}")
    for key_id, count in sorted(summary.by_key_id.items()):
        print(f"  key_id={key_id} count={count}")
    print(f"current_key_id={ring.write_key_id}")
    print(f"previous_key_id={ring.previous.key_id if ring.previous else 'none'}")
    print(f"index_retire_safe={'yes' if summary.index_safe_to_retire(ring.write_key_id) else 'no'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rotate the audit archive cold-copy key (register -> switch write -> retire old)."
    )
    parser.add_argument("--new-secret-file", default=None, help="file containing the new key material (no trailing newline needed)")
    parser.add_argument("--new-secret-stdin", action="store_true", help="read new key material from stdin (first line)")
    parser.add_argument("--retire-old", action="store_true", help="verify every encrypted object decrypts with the current key only")
    parser.add_argument("--sample", type=int, default=10, help="how many blockers to print (default: 10)")
    parser.add_argument("--tenant-id", default=None, help="restrict to one tenant")
    parser.add_argument("--object-root", default=None, help="local object store root (default: FILE_STORAGE_DIR)")
    parser.add_argument("--database-url", default=None, help="override DATABASE_URL")
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.builtin_plugins.object_store import LocalObjectStore
    from app.builtin_plugins.secret_provider import LocalSecretProvider
    from app.config import get_settings

    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - 配置不全时给出可读结论
        print(f"cannot load settings: {exc}", file=sys.stderr)
        return 2

    provider = LocalSecretProvider()
    try:
        config = AuditExportConfig.from_settings(settings)
        config.validate()  # 配置非法（如 previous 与 current 同引用）必须显式失败
    except Exception as exc:  # noqa: BLE001 - 配置非法一律显式失败，不带着错配置去动密钥
        print(f"invalid audit archive config: {exc}", file=sys.stderr)
        return 2

    if args.new_secret_file or args.new_secret_stdin:
        try:
            ref = register_new_secret(provider, _load_new_secret(args))
        except Exception as exc:  # noqa: BLE001
            print(f"cannot register new archive key: {exc}", file=sys.stderr)
            return 2
        previous = config.archive_secret_ref
        material = provider.decrypt(ref[len(SECRET_PREFIX) :])
        print("mode=register")
        print(f"new key registered, key_id={key_id_of(derive_key(material))}")
        print("apply these to the environment, then restart (do NOT commit them):")
        print(f"AUDIT_ARCHIVE_SECRET_REF={ref}")
        if previous:
            print(f"AUDIT_ARCHIVE_PREVIOUS_SECRET_REF={previous}")
        return 0

    try:
        ring = build_key_ring(
            enabled=config.archive_encryption_enabled,
            secret_ref=config.archive_secret_ref,
            previous_secret_ref=config.archive_previous_secret_ref,
            provider=provider,
        )
    except Exception as exc:  # noqa: BLE001 - 密钥装配失败（含 AI_AUDIT_ARCHIVE_KEY_MISSING）
        print(f"cannot assemble archive key ring: {getattr(exc, 'code', 'ERROR')} {exc}", file=sys.stderr)
        return 2
    if ring is None:
        print("archive encryption is disabled: nothing to rotate", file=sys.stderr)
        return 2

    if args.retire_old:
        engine = create_engine(args.database_url or settings.database_url, future=True)
        session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        object_root = Path(args.object_root) if args.object_root else Path(settings.file_storage_dir)
        store = LocalObjectStore(object_root)
        tenant_id = UUID(args.tenant_id) if args.tenant_id else None
        try:
            refs = collect_archive_objects(session_factory, tenant_id=tenant_id)
            check = check_retire(refs, store, ring)
        except Exception as exc:  # noqa: BLE001 - 探测失败按 2 退出，区别于"被阻塞"
            print(f"retire check failed: {exc}", file=sys.stderr)
            return 2
        finally:
            engine.dispose()

        print("mode=retire-old")
        _print_usage(summarize(refs), ring)
        print(
            f"checked={check.checked} readable_with_current={check.readable_with_current} "
            f"missing={len(check.missing)} blocked={len(check.blockers)} warnings={len(check.warnings)}"
        )
        sample = max(0, args.sample)
        for warning in check.warnings[:sample]:
            print(f"  ! {warning.object_key} batch={warning.batch_id} {warning.reason}")
        if not check.safe:
            print(f"blocked={KEY_MISSING} retire is NOT allowed, fix these objects first:")
            for blocker in check.blockers[:sample]:
                print(f"  - {blocker.object_key} batch={blocker.batch_id} {blocker.reason}")
            if len(check.blockers) > sample:
                print(f"  ... {len(check.blockers) - sample} more")
            return 3
        if check.missing:
            print(f"note: {len(check.missing)} referenced object(s) are missing (dangling, see reconcile script)")
        print("retire is safe: every encrypted object decrypts with the current key")
        print("you may now drop the read-only key: AUDIT_ARCHIVE_PREVIOUS_SECRET_REF=")
        return 0

    engine = create_engine(args.database_url or settings.database_url, future=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    tenant_id = UUID(args.tenant_id) if args.tenant_id else None
    try:
        refs = collect_archive_objects(session_factory, tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001
        print(f"cannot read archive index: {exc}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()
    print("mode=status")
    _print_usage(summarize(refs), ring)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
