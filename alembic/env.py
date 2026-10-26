"""Alembic environment for the Z-Torque database."""
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# 插件私有表（方案 b）：模型定义随插件目录，但注册进同一个 Base.metadata，
# 因此迁移链（alembic/versions）自动覆盖它们；内核 app/models 不反向依赖插件。
from app.builtin_plugins.audit_export_archive import models as _audit_export_models  # noqa: F401, E402
from app.config import get_settings
from app.db.alembic_support import compare_type
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
    # disable_existing_loggers 必须为 False：迁移可能在应用进程内被调用
    # （测试 / 启动时自检），默认值会把 app.* 的 logger 全部禁用，
    # 表现为后续 caplog 断言突然拿不到日志。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# URL 解析优先级：调用方显式注入（测试/临时库）> DATABASE_URL（settings）> alembic.ini。
# 空字符串即「未注入」，此时才回落到 settings，避免 ini 默认值抢在环境变量之前。
_injected_url = (config.get_main_option("sqlalchemy.url") or "").strip()
config.set_main_option(
    "sqlalchemy.url",
    (_injected_url or get_settings().database_url).replace("%", "%%"),
)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=compare_type,
    )
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
            compare_type=compare_type,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
