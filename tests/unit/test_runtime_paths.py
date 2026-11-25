from pathlib import Path
import sys

from app.runtime_paths import (
    default_database_url,
    default_data_dir,
    default_env_file,
    default_sandbox_python,
    default_user_path,
    resource_dir,
)


def test_frozen_paths_use_local_appdata_and_bundle_resources(tmp_path, monkeypatch):
    local = tmp_path / "Local"
    bundle = tmp_path / "bundle"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("ZT_DATA_DIR", raising=False)

    assert default_data_dir() == local / "Z-Torque"
    assert default_database_url() == f"sqlite:///{(local / 'Z-Torque' / 'z_torque.db').resolve().as_posix()}"
    assert default_env_file() == local / "Z-Torque" / ".env"
    assert default_user_path("files") == local / "Z-Torque" / "files"
    assert resource_dir() == bundle
    assert default_sandbox_python() == bundle / "runtime" / "python.exe"


def test_frozen_data_dir_can_be_overridden(tmp_path, monkeypatch):
    override = tmp_path / "portable-data"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("ZT_DATA_DIR", str(override))
    assert default_data_dir() == override


def test_source_paths_keep_development_layout(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert default_database_url() == "sqlite:///./data/z_torque.db"
    assert default_user_path("secrets") == Path("./data/secrets")
    assert default_user_path("md-skills") == Path("./md-skills")
