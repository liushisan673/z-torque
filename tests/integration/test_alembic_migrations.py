"""迁移链门禁：迁移产物必须与 ORM 模型一致，且降级/升级可往返。

背景：UUID 列在模型侧（``GUID`` / 隐式 ``Uuid``）与历史迁移（``String(36)``、
PG 原生 ``uuid``）之间存储形态不同，autogenerate 会长期报几十条
"Detected type change from VARCHAR(36) to GUID(36)" 的类型漂移，
使 ``alembic check`` 无法作为门禁。``app/db/alembic_support.compare_type``
给出统一比较策略，这里用与 CLI 完全相同的策略断言零漂移，
防止再出现「文档说零漂移、实际又飘」。
"""

import os
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

import app.models  # noqa: F401  # 注册全部模型到 Base.metadata
from alembic import command
from app.db.alembic_support import compare_type
from app.db.base import Base

ROOT = Path(__file__).resolve().parents[2]


def _config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def _sqlite_url(tmp_path: Path, name: str) -> str:
    return f"sqlite:///{(tmp_path / name).as_posix()}"


def test_migrations_match_models(tmp_path):
    url = _sqlite_url(tmp_path, "drift.db")
    command.upgrade(_config(url), "head")
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"compare_type": compare_type})
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()
    assert diff == [], f"迁移与模型存在漂移: {diff}"


def test_migration_round_trip(tmp_path):
    url = _sqlite_url(tmp_path, "roundtrip.db")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn)
            assert context.get_current_revision() is not None
    finally:
        engine.dispose()


_PG_URL = os.getenv("ZTORQUE_MIGRATION_CHECK_URL")


@pytest.mark.skipif(not _PG_URL, reason="未设置 ZTORQUE_MIGRATION_CHECK_URL，跳过 PG 干净库校验")
def test_postgres_schema_matches_models():
    """SQLite 没有时区语义，时间戳类漂移只能在 PG 上暴露。

    CI 用法：先用 alembic 在一个空库上 upgrade head，再把该库地址塞进
    ZTORQUE_MIGRATION_CHECK_URL；本用例只读，不建表也不清表。
    """
    engine = create_engine(_PG_URL)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"compare_type": compare_type})
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()
    assert diff == [], f"PG 上迁移与模型存在漂移: {diff}"
