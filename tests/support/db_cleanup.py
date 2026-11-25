"""Test-only database cleanup helpers.

Production SQLite connections always keep foreign-key enforcement enabled.
Some legacy fixtures intentionally remove parent rows without deleting every
dependent row first; disable FK checks only on the dedicated cleanup connection.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import engine


@contextmanager
def cleanup_session() -> Iterator[Session]:
    if engine.dialect.name == "sqlite":
        with engine.connect() as connection:
            raw = connection.connection.driver_connection
            # checkout hooks have already applied production PRAGMAs; change this one
            # test-cleanup connection before opening the transaction.
            raw.execute("PRAGMA foreign_keys=OFF")
            session = Session(bind=connection, autoflush=False, expire_on_commit=False)
            try:
                with session.begin():
                    yield session
            finally:
                session.close()
                raw.execute("PRAGMA foreign_keys=ON")
        return

    # PostgreSQL CI uses a dedicated superuser test database; mirror the existing
    # fk_off/fk_on helper but guarantee restoration even if a fixture raises.
    with Session(bind=engine, autoflush=False, expire_on_commit=False) as session:
        with session.begin():
            if engine.dialect.name == "postgresql":
                session.execute(text("SET session_replication_role = replica"))
            try:
                yield session
            finally:
                if engine.dialect.name == "postgresql":
                    session.execute(text("SET session_replication_role = origin"))
