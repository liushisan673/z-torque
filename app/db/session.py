from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def _database_url() -> str:
    url = get_settings().database_url
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///" )).parent.mkdir(parents=True, exist_ok=True)
    return url


engine = create_engine(_database_url(), future=True, pool_pre_ping=True)


def _ensure_sqlite_columns() -> None:
    """项目无迁移机制：对既有 SQLite 库幂等补充新增列（可空列，ALTER 安全）。"""
    url = _database_url()
    if not url.startswith("sqlite:///"):
        return
    path = url.removeprefix("sqlite:///")
    if not Path(path).exists():
        return
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(zr_user)")}
        if existing:
            if "display_name" not in existing:
                conn.execute("ALTER TABLE zr_user ADD COLUMN display_name VARCHAR(128) NULL")
            if "last_login_at" not in existing:
                conn.execute("ALTER TABLE zr_user ADD COLUMN last_login_at TIMESTAMP NULL")
            conn.commit()
    finally:
        conn.close()


_ensure_sqlite_columns()


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def check_database() -> bool:
    with engine.connect() as connection:
        return connection.execute(text("SELECT 1")).scalar_one() == 1
