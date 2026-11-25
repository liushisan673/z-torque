"""Windows single-machine bootstrap: migrate, initialize local admin, then serve the bundled console."""

from __future__ import annotations

import logging
import threading
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config

from app.config import get_settings
from app.runtime_paths import is_frozen, resource_dir


def _configure_file_logging(data_dir: Path, level: str) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "z-torque.log"
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if any(getattr(handler, "baseFilename", None) == str(log_path) for handler in root.handlers):
        return
    handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    root.addHandler(handler)


def _upgrade_database() -> None:
    config_path = resource_dir() / "alembic.ini"
    if not config_path.is_file():
        raise RuntimeError(f"Alembic configuration is missing: {config_path}")
    config = Config(str(config_path))
    config.set_main_option("script_location", str(resource_dir() / "alembic"))
    config.set_main_option("prepend_sys_path", str(resource_dir()))
    # env.py resolves the URL from Settings; keep ini empty to avoid a stale hard-coded path.
    config.set_main_option("sqlalchemy.url", "")
    command.upgrade(config, "head")


def main() -> None:
    settings = get_settings()
    _configure_file_logging(settings.data_dir, settings.log_level)

    if is_frozen() and settings.app_host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("The single-machine desktop build only supports loopback binding.")

    _upgrade_database()

    # Creates the default tenant, permissions, roles and initial administrator idempotently.
    from app.cli import init_admin

    init_admin()

    # Import the application only after migrations and the first administrator exist.
    from app.main import app

    url_host = "127.0.0.1" if settings.app_host == "localhost" else settings.app_host
    browser_url = f"http://{url_host}:{settings.app_port}/"
    if settings.serve_console:
        threading.Timer(1.5, lambda: webbrowser.open(browser_url)).start()

    uvicorn.run(
        app,
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
