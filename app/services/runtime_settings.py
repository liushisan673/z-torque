"""本机运行时偏好：与身份无关、可在设置页即时切换的少量开关。

配置落在本机数据目录的 JSON 文件，随桌面包的用户数据一起备份；
缺省值仍来自 app.config.Settings，因此服务端部署行为保持不变。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from app.config import get_settings

_FILENAME = "runtime-settings.json"
_LOCK = threading.Lock()


def _settings_path() -> Path:
    settings = get_settings()
    return Path(settings.file_storage_dir).expanduser().resolve().parent / _FILENAME


def _read_unlocked() -> dict[str, Any]:
    path = _settings_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get_file_change_auto_keep() -> bool:
    """文件直出开关：未显式设置时沿用服务端默认值（默认关，先提案后 Keep）。"""
    with _LOCK:
        value = _read_unlocked().get("file_change_auto_keep")
    if isinstance(value, bool):
        return value
    return bool(get_settings().file_change_auto_keep)


def set_file_change_auto_keep(value: bool) -> bool:
    with _LOCK:
        path = _settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _read_unlocked()
        data["file_change_auto_keep"] = bool(value)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    return bool(value)
