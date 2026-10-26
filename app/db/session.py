import sqlite3
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
    """历史 SQLite 开发库的兜底补列。

    正式表结构以 alembic 迁移为准（alembic upgrade head）。这里只服务尚未执行迁移的本地
    dev 库，做幂等 ALTER；非 SQLite 方言不做任何打补丁，避免掩盖迁移缺失。
    """
    url = _database_url()
    if not url.startswith("sqlite:///"):
        return
    path = url.removeprefix("sqlite:///" )
    if not Path(path).exists():
        return

    conn = sqlite3.connect(path)
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(zr_user)")}
        if existing:
            if "display_name" not in existing:
                conn.execute("ALTER TABLE zr_user ADD COLUMN display_name VARCHAR(128) NULL")
            if "last_login_at" not in existing:
                conn.execute("ALTER TABLE zr_user ADD COLUMN last_login_at TIMESTAMP NULL")
            _normalize_uuid_formats(conn)
            conn.commit()
    finally:
        conn.close()


# UUID 列格式统一（幂等数据迁移）：历史模型中未声明 GUID 类型的 UUID 列被 SQLAlchemy
# 原生 Uuid 存成 32 位无连字符，与 GUID 主键（36 位带连字符）不一致，裸 SQL 关联会失配；
# 模型层已统一补声明 GUID，这里把存量值重格式化为 36 位。
_UUID_REFORMAT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("zr_agent", "source_agent_version_id"),
    ("zr_agent", "current_published_version_id"),
    ("zr_agent_version_skill", "tenant_id"),
    ("zr_audit_log", "resource_id"),
    ("zr_message", "conversation_id"),
    ("zr_outbox", "tenant_id"),
    ("zr_outbox", "aggregate_id"),
    ("zr_skill", "current_published_version_id"),
    ("zr_skill_version_tool", "tenant_id"),
    ("zr_task", "parent_task_id"),
    ("zr_task", "root_task_id"),
    ("zr_task", "conversation_id"),
    ("zr_task", "trigger_message_id"),
    ("zr_task", "message_id"),
    ("zr_task", "result_message_id"),
    ("zr_task", "agent_id"),
    ("zr_task", "agent_version_id"),
    ("zr_task", "delegation_id"),
    ("zr_task_event", "tenant_id"),
    ("zr_tool", "current_published_version_id"),
)


def _normalize_uuid_formats(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, column in _UUID_REFORMAT_COLUMNS:
        if table not in tables:
            continue
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            continue
        conn.execute(
            f"UPDATE {table} SET {column} = "
            f"substr({column},1,8)||'-'||substr({column},9,4)||'-'||substr({column},13,4)||'-'||substr({column},17,4)||'-'||substr({column},21,12) "
            f"WHERE {column} IS NOT NULL AND LENGTH({column}) = 32"
        )


_ensure_sqlite_columns()


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record):
    # 只在 SQLite 连接上执行 PRAGMA：PostgreSQL/MySQL 连接执行会直接语法错误，
    # 使引擎在首次取连接时就失败（等于该方言完全不可用）。
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
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
