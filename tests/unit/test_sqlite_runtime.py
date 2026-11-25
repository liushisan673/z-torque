import pytest

from app.db.session import engine


def test_sqlite_runtime_uses_wal_and_bounded_lock_wait():
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite runtime settings only apply to the embedded application database")

    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar_one()
        wal_autocheckpoint = connection.exec_driver_sql("PRAGMA wal_autocheckpoint").scalar_one()

    assert str(journal_mode).lower() == "wal"
    assert busy_timeout == 5000
    assert foreign_keys == 1
    assert synchronous == 1  # NORMAL
    assert wal_autocheckpoint == 1000
