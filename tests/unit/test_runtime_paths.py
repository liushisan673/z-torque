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



def test_frozen_runtime_generates_persistent_secrets_and_first_run_credentials(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    import app.config as config

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.delenv("ZT_DATA_DIR", raising=False)
    for key in ("JWT_SECRET_KEY", "SECRET_STORE_KEY", "ADMIN_INITIAL_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    config._ensure_frozen_runtime_secrets()
    data_dir = tmp_path / "Local" / "Z-Torque"
    stored = __import__("json").loads((data_dir / "runtime-secrets.json").read_text(encoding="utf-8"))
    assert len(stored["jwt_secret_key"]) >= 32
    Fernet(stored["secret_store_key"].encode("ascii"))
    assert len(stored["admin_initial_password"]) >= 12
    credentials = (data_dir / "FIRST_RUN_CREDENTIALS.txt").read_text(encoding="utf-8")
    assert stored["admin_initial_password"] in credentials

    first_jwt = stored["jwt_secret_key"]
    config._ensure_frozen_runtime_secrets()
    reloaded = __import__("json").loads((data_dir / "runtime-secrets.json").read_text(encoding="utf-8"))
    assert reloaded["jwt_secret_key"] == first_jwt
