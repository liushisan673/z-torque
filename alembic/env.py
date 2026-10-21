"""Alembic environment for the Z-Torque database."""
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.config import get_settings
from app.db.base import Base
from app.models import (  # noqa: F401
    Agent,
    AgentVersion,
    AgentVersionSkill,
    Conversation,
    Message,
    Permission,
    Role,
    User,
)

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        if is_sqlite:
            # SQLite 的 ALTER 能力有限：batch 模式靠「建新表→拷数据→删旧表→改名」实现加约束，
            # 外键开启时删除被其他表引用的父表会直接失败。迁移连接保持关闭，
            # 运行期约束由 app/db/session.py 的 PRAGMA foreign_keys=ON 负责。
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
