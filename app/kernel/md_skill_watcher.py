from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from app.kernel.md_skill_registry import ScanResult, scan_directory

logger = logging.getLogger("z_torque.md_skill")


@dataclass
class MdSkillScanState:
    running: bool = False
    last_started_at: str | None = None
    last_finished_at: str | None = None
    last_result: ScanResult | None = None
    last_error: str | None = None


class MdSkillWatcher:
    def __init__(self, root: str | Path, tenant_id: UUID, *, interval_seconds: float = 15.0, enabled: bool = True, session_factory: Callable[[], Session] | None = None) -> None:
        self.root = Path(root)
        self.tenant_id = tenant_id
        self.interval_seconds = interval_seconds
        self.enabled = enabled
        # 会话工厂由装配根注入（阶段00 §3.8：内核不直接依赖具体 Session 实现）
        self.session_factory = session_factory
        self.state = MdSkillScanState()
        self._task: asyncio.Task[None] | None = None
        self._fingerprint: tuple[tuple[str, int, int], ...] = ()

    def _files(self) -> tuple[tuple[str, int, int], ...]:
        if not self.root.exists():
            return ()
        return tuple(sorted((str(path.relative_to(self.root)), path.stat().st_size, path.stat().st_mtime_ns) for path in self.root.rglob("*") if path.is_file() and not path.is_symlink()))

    async def scan(self) -> ScanResult:
        from datetime import datetime, timezone
        if self.session_factory is None:
            raise RuntimeError("MdSkillWatcher requires session_factory (injected from assembly)")
        self.state.running = True
        self.state.last_started_at = datetime.now(timezone.utc).isoformat()
        db = self.session_factory()
        try:
            result = scan_directory(db, tenant_id=self.tenant_id, root=self.root)
            db.commit()
            self.state.last_result = result
            self.state.last_error = None
            self._fingerprint = self._files()
            return result
        except Exception as exc:
            db.rollback()
            self.state.last_error = "MD-Skill scan failed"
            logger.exception("MD-Skill scan failed", exc_info=exc)
            raise
        finally:
            db.close()
            self.state.running = False
            self.state.last_finished_at = datetime.now(timezone.utc).isoformat()

    async def _run(self) -> None:
        while True:
            try:
                if self._files() != self._fingerprint:
                    await self.scan()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MD-Skill watcher iteration failed")
            await asyncio.sleep(self.interval_seconds)

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        await self.scan()
        self._task = asyncio.create_task(self._run(), name="md-skill-watcher")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
