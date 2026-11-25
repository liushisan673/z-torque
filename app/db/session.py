import sqlite3
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


_SQLITE_BUSY_TIMEOUT_MS = 30_000
_SQLITE_CACHE_SIZE_KIB = 8_192


def _database_url() -> str:
    url = get_settings().database_url
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        database = parsed.database
        if database and database != ":memory:" and not database.startswith("file:"):
            Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)
    return url


def _build_engine(url: str):
    parsed = make_url(url)
    connect_args = {}
    if parsed.get_backend_name() == "sqlite":
        # 显式固定线程与锁等待语义：SQLAlchemy 的默认值会随 URL 形态变化，
        # 单机任务 worker 与 API 线程并发时必须允许连接跨线程使用，并给短写锁留出等待窗口。
        connect_args = {
            "check_same_thread": False,
            "timeout": _SQLITE_BUSY_TIMEOUT_MS / 1000,
        }
    return create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)


engine = _build_engine(_database_url())


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


def _sqlite_pragmas(dbapi_connection, _connection_record=None) -> None:
    """每条 SQLite 连接的稳定运行参数；非 SQLite 连接绝不执行 PRAGMA。"""
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute(f"PRAGMA cache_size=-{_SQLITE_CACHE_SIZE_KIB}")
    finally:
        cursor.close()


def _sqlite_wal_first_connect(dbapi_connection, _connection_record=None) -> None:
    """WAL 是数据库级设置，只在首条连接建立时设置，避免每个连接重复切换 journal mode。"""
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


event.listen(engine, "connect", _sqlite_pragmas)
event.listen(engine, "first_connect", _sqlite_wal_first_connect)


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
