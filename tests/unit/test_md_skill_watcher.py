from pathlib import Path
from uuid import uuid4

import pytest

from app.kernel.md_skill_watcher import MdSkillWatcher


@pytest.mark.asyncio
async def test_watcher_detects_initial_directory(tmp_path: Path):
    watcher = MdSkillWatcher(tmp_path, uuid4(), enabled=False)
    assert watcher._files() == ()
    await watcher.start()
    assert watcher._task is None


@pytest.mark.asyncio
async def test_watcher_stop_without_start_is_safe(tmp_path: Path):
    watcher = MdSkillWatcher(tmp_path, uuid4())
    await watcher.stop()
