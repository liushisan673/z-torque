"""事务式补丁实现（Phase 0 §2.3）：挂到 sandbox.driver Port 上的窄扩展。

只做文件系统操作，不碰 DB/审计（内核 gateway 负责落 artifact 与审计）。
不变量：
- old_text 必须唯一匹配；提交前 hash 复校验（防 TOCTOU）；
- 多文件先全部 prepare 再写入，失败逐项回滚；回滚失败收敛为 PARTIAL 并上报告警；
- 符号链接：父目录链逐级 realpath 校验 + O_NOFOLLOW 打开目标；
- 同一路径用独立锁文件串行化，避免并发写。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from app.plugin_sdk import (
    PATCH_COMMITTED,
    PATCH_PARTIAL,
    PATCH_ROLLED_BACK,
    ApiError,
    PatchFileResult,
    PatchReport,
    PatchSpec,
    SandboxScope,
)
from app.plugin_sdk.observability import emit_event


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_target(raw: str | Path, roots: Sequence[str]) -> Path:
    """解析目标路径：父目录链不得含符号链接，realpath 后必须落在 scope 内。"""
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ApiError("AI_SANDBOX_DENIED", "patch path must be absolute", 403)
    for parent in candidate.parents:
        if parent.is_symlink():
            raise ApiError("AI_SANDBOX_DENIED", "patch parent directory is a symlink", 403)
    resolved = candidate.resolve()
    for item in roots:
        root = Path(item).expanduser().resolve()
        if resolved == root or root in resolved.parents:
            return resolved
    raise ApiError("AI_SANDBOX_DENIED", "patch path is outside sandbox scope", 403)


@contextmanager
def _locked_file(path: Path) -> Iterator[object]:
    """按目标路径加锁并返回文本句柄；短临界区，退出即解锁。"""
    lock_path = path.with_name(path.name + ".ztlock")
    with open(lock_path, "a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)

            def unlock() -> None:
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

            def unlock() -> None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        try:
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            with os.fdopen(fd, "r+", encoding="utf-8", newline="") as handle:
                yield handle
        finally:
            unlock()
    try:
        lock_path.unlink()
    except OSError:  # pragma: no cover - 清理尽力而为
        pass


def _read_text(path: Path) -> str:
    with _locked_file(path) as handle:
        return handle.read()


def _write_text(path: Path, text: str) -> None:
    with _locked_file(path) as handle:
        handle.seek(0)
        handle.write(text)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


def _rollback(applied: Sequence[tuple[Path, str]]) -> list[str]:
    failures: list[str] = []
    for path, before in reversed(applied):
        try:
            _write_text(path, before)
        except OSError:  # pragma: no cover - 回滚失败才进入 PARTIAL
            failures.append(str(path))
    return failures


class FilePatchMixin:
    """提供 apply_patch 的具体文件实现；与 process/container 驱动共用。"""

    async def apply_patch(self, scope: SandboxScope, *, patches: list[PatchSpec], base_dir: Path) -> PatchReport:
        if not patches:
            raise ApiError("AI_PATCH_INVALID", "patches must not be empty", 422)
        if not scope.permits(base_dir):
            raise ApiError("AI_SANDBOX_DENIED", "patch base directory is outside sandbox scope", 403)
        roots = scope.allowed_roots
        prepared: list[tuple[Path, str, str, str]] = []
        for spec in patches:
            target = _safe_target(spec.path, roots)
            before = _read_text(target)
            before_hash = _sha256(before)
            if before_hash != spec.expected_hash:
                raise ApiError("AI_PATCH_CONFLICT", f"patch target changed before commit: {target.name}", 409)
            if before.count(spec.old_text) != 1:
                raise ApiError("AI_PATCH_INVALID", "old_text must match exactly once", 422)
            prepared.append((target, before, before_hash, before.replace(spec.old_text, spec.new_text, 1)))

        applied: list[tuple[Path, str]] = []
        try:
            for target, before, before_hash, after in prepared:
                # 提交前复校验：prepare 与 commit 之间的并发修改在这里被拦下
                if _sha256(_read_text(target)) != before_hash:
                    raise ApiError("AI_PATCH_CONFLICT", f"patch target changed before commit: {target.name}", 409)
                _write_text(target, after)
                applied.append((target, before))
        except ApiError as exc:
            failures = _rollback(applied)
            if failures:
                emit_event("sandbox.patch_partial", {"count": len(failures), "error_code": exc.code})
                return PatchReport(status=PATCH_PARTIAL, error_code=exc.code, message=f"rollback failed for {len(failures)} file(s)")
            return PatchReport(status=PATCH_ROLLED_BACK, error_code=exc.code, message=exc.message)
        except OSError as exc:
            failures = _rollback(applied)
            if failures:
                emit_event("sandbox.patch_partial", {"count": len(failures), "error_code": "AI_PATCH_WRITE_FAILED"})
                return PatchReport(status=PATCH_PARTIAL, error_code="AI_PATCH_WRITE_FAILED", message=str(exc))
            return PatchReport(status=PATCH_ROLLED_BACK, error_code="AI_PATCH_WRITE_FAILED", message=str(exc))
        return PatchReport(
            status=PATCH_COMMITTED,
            files=[
                PatchFileResult(path=str(target), before_hash=before_hash, after_hash=_sha256(after), before_text=before, after_text=after)
                for target, before, before_hash, after in prepared
            ],
        )
