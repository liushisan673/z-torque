"""Runtime-aware paths for development and frozen Windows desktop builds.

Development keeps the repository's familiar relative paths. A frozen executable
stores mutable user data under LOCALAPPDATA and reads bundled assets from
PyInstaller's extraction/resource directory, never from the process CWD.
"""

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def default_data_dir() -> Path:
    override = os.getenv("ZT_DATA_DIR")
    if override:
        path = Path(override).expanduser()
        if is_frozen() and not path.is_absolute():
            path = Path(sys.executable).resolve().parent / path
        return path
    if is_frozen():
        base = os.getenv("LOCALAPPDATA")
        if not base:
            base = str(Path.home() / "AppData" / "Local")
        return Path(base).expanduser() / "Z-Torque"
    return Path("./data")


def resource_dir() -> Path:
    """Directory containing read-only bundled application resources."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[1]


def default_env_file() -> Path:
    if is_frozen():
        return default_data_dir() / ".env"
    return resource_dir() / ".env"


def default_database_url() -> str:
    if is_frozen():
        database = (default_data_dir() / "z_torque.db").resolve()
        return f"sqlite:///{database.as_posix()}"
    return "sqlite:///./data/z_torque.db"


def default_user_path(name: str) -> Path:
    if is_frozen():
        return default_data_dir() / name
    if name == "md-skills":
        return Path("./md-skills")
    if name == "console/dist":
        return Path("./console/dist")
    return Path("./data") / name


def default_sandbox_python() -> Path:
    """Interpreter for isolated MD-Skill execution; frozen builds bundle python.exe."""
    if is_frozen():
        return resource_dir() / "runtime" / "python.exe"
    return Path(sys.executable)
