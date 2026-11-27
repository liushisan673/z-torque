import sys
from pathlib import Path

import desktop_entry


def test_prepare_environment_uses_user_data_dir_and_sqlite_by_default(tmp_path, monkeypatch):
    if sys.platform == "win32":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    for key in (
        "DATABASE_URL",
        "JWT_SECRET_KEY",
        "SECRET_STORE_KEY",
        "ADMIN_INITIAL_PASSWORD",
        "ADMIN_INITIAL_USERNAME",
        "FILE_STORAGE_DIR",
        "SECRET_STORE_DIR",
        "MD_SKILLS_DIR",
        "APP_ENV",
        "APP_DEBUG",
        "APP_HOST",
        "APP_PORT",
        "SERVE_CONSOLE",
        "CONSOLE_DIST_DIR",
    ):
        monkeypatch.delenv(key, raising=False)

    data_dir, database_was_missing = desktop_entry._prepare_environment()
    values = desktop_entry._read_env(data_dir / "desktop.env")

    assert database_was_missing is True
    assert data_dir == (tmp_path / "ZTorque").resolve()
    assert values["DATABASE_URL"].startswith("sqlite:///")
    assert Path(values["FILE_STORAGE_DIR"]).is_dir()
    assert Path(values["SECRET_STORE_DIR"]).is_dir()
    assert Path(values["MD_SKILLS_DIR"]).is_dir()
    assert len(values["JWT_SECRET_KEY"]) >= 32
    assert len(values["SECRET_STORE_KEY"]) > 30
    assert values["ADMIN_INITIAL_PASSWORD"] == "admin123"
    assert values["ADMIN_INITIAL_USERNAME"] == "admin"
    assert values["ALLOW_DEFAULT_ADMIN_PASSWORD"] == "true"
    assert values["APP_ENV"] == "production"
    assert values["APP_DEBUG"] == "false"
    assert values["APP_HOST"] == "127.0.0.1"
    assert desktop_entry.os.environ["SERVE_CONSOLE"] == "true"


def test_builtin_admin_password_stays_fixed_on_relaunch(tmp_path, monkeypatch):
    if sys.platform == "win32":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    data_dir, _ = desktop_entry._prepare_environment()
    values = desktop_entry._read_env(data_dir / "desktop.env")
    assert values["ADMIN_INITIAL_PASSWORD"] == "admin123"

    # 二次启动应保留既有数据，不重复导入种子，也不改内置账号配置。
    data_dir, seeded_again = desktop_entry._prepare_environment()
    assert seeded_again is False
    assert desktop_entry._read_env(data_dir / "desktop.env")["ADMIN_INITIAL_PASSWORD"] == "admin123"


def test_frozen_worker_mode_executes_script_without_starting_web_app(tmp_path, monkeypatch):
    script = tmp_path / "worker.py"
    script.write_text("print('ok')", encoding="utf-8")
    called = {}

    def fake_run_path(path, *, run_name):
        called["path"] = path
        called["run_name"] = run_name

    monkeypatch.setattr(desktop_entry.runpy, "run_path", fake_run_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "argv", ["ZTorque.exe", "--zt-sandbox-worker", str(script)])

    assert desktop_entry._worker_mode() is True
    assert called == {"path": str(script.resolve()), "run_name": "__main__"}
    assert sys.argv == [str(script.resolve())]
    assert sys.path[0] == str(script.resolve().parent)
