from sqlalchemy import create_engine, event

from app.db.session import _build_engine, _sqlite_pragmas, _sqlite_wal_first_connect


def test_sqlite_engine_uses_wal_and_bounded_wait_pragmas(tmp_path):
    database = tmp_path / "runtime.db"
    engine = _build_engine(f"sqlite:///{database.as_posix()}")
    event.listen(engine, "connect", _sqlite_pragmas)
    event.listen(engine, "first_connect", _sqlite_wal_first_connect)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 30_000
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA temp_store").scalar_one() == 2
            assert connection.exec_driver_sql("PRAGMA cache_size").scalar_one() == -8_192
            assert connection.exec_driver_sql("PRAGMA wal_autocheckpoint").scalar_one() == 1_000
    finally:
        engine.dispose()


def test_sqlite_pragmas_ignore_non_sqlite_connections():
    # PostgreSQL/MySQL DBAPI connections must not receive SQLite-only PRAGMA statements.
    _sqlite_pragmas(object())
    _sqlite_wal_first_connect(object())


def test_sqlite_engine_builder_sets_cross_thread_connection_options(tmp_path):
    engine = _build_engine(f"sqlite:///{(tmp_path / 'cross-thread.db').as_posix()}")
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
    finally:
        engine.dispose()
