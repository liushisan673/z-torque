"""Align secondary indexes and foreign keys with ORM metadata.

历史迁移用手写 ``op.create_table`` 建表时漏掉了模型声明的二级索引与部分外键，
导致 ``Base.metadata`` 与迁移产物长期漂移（38 个索引、7 个外键）。
本迁移一次性补齐，使 ``compare_metadata`` 只剩 SQLite 下 GUID/Uuid 的渲染噪声。

SQLite 不支持 ``ALTER TABLE ADD CONSTRAINT``，加外键走 batch 重建表；
``env.py`` 已在迁移连接上关闭外键，避免重建被引用的父表时失败。
"""
from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5c1f7a92b40"
down_revision: str | None = "d4e9b2c7f1a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (表, 索引名, 列)
_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("zr_agent", "ix_zr_agent_current_published_version_id", ("current_published_version_id",)),
    ("zr_agent", "ix_zr_agent_is_default_parent", ("is_default_parent",)),
    ("zr_agent_version", "ix_zr_agent_version_tenant_id", ("tenant_id",)),
    ("zr_agent_version_skill", "ix_zr_agent_version_skill_tenant_id", ("tenant_id",)),
    ("zr_confirmation", "ix_zr_confirmation_status", ("status",)),
    ("zr_confirmation", "ix_zr_confirmation_task_id", ("task_id",)),
    ("zr_confirmation", "ix_zr_confirmation_tenant_id", ("tenant_id",)),
    ("zr_connector", "ix_zr_connector_status", ("status",)),
    ("zr_connector", "ix_zr_connector_tenant_id", ("tenant_id",)),
    ("zr_connector_tool", "ix_zr_connector_tool_connector_id", ("connector_id",)),
    ("zr_connector_tool", "ix_zr_connector_tool_tenant_id", ("tenant_id",)),
    ("zr_connector_tool", "ix_zr_connector_tool_tool_id", ("tool_id",)),
    ("zr_conversation", "ix_zr_conversation_agent_id", ("agent_id",)),
    ("zr_conversation", "ix_zr_conversation_tenant_id", ("tenant_id",)),
    ("zr_file", "ix_zr_file_conversation_id", ("conversation_id",)),
    ("zr_file", "ix_zr_file_owner_user_id", ("owner_user_id",)),
    ("zr_file", "ix_zr_file_tenant_id", ("tenant_id",)),
    ("zr_memory_short_term", "ix_zr_memory_short_term_agent_id", ("agent_id",)),
    ("zr_memory_short_term", "ix_zr_memory_short_term_tenant_id", ("tenant_id",)),
    ("zr_memory_short_term", "ix_zr_memory_short_term_user_id", ("user_id",)),
    ("zr_message", "ix_zr_message_tenant_id", ("tenant_id",)),
    ("zr_skill", "ix_zr_skill_current_published_version_id", ("current_published_version_id",)),
    ("zr_skill", "ix_zr_skill_owner_user_id", ("owner_user_id",)),
    ("zr_skill", "ix_zr_skill_tenant_id", ("tenant_id",)),
    ("zr_skill_execution_log", "ix_zr_skill_execution_log_skill_version_id", ("skill_version_id",)),
    ("zr_skill_execution_log", "ix_zr_skill_execution_log_task_id", ("task_id",)),
    ("zr_skill_execution_log", "ix_zr_skill_execution_log_tenant_id", ("tenant_id",)),
    ("zr_skill_version", "ix_zr_skill_version_skill_id", ("skill_id",)),
    ("zr_skill_version", "ix_zr_skill_version_status", ("status",)),
    ("zr_skill_version", "ix_zr_skill_version_tenant_id", ("tenant_id",)),
    ("zr_skill_version_tool", "ix_zr_skill_version_tool_tenant_id", ("tenant_id",)),
    ("zr_task_checkpoint", "ix_zr_task_checkpoint_tenant_id", ("tenant_id",)),
    ("zr_tool", "ix_zr_tool_current_published_version_id", ("current_published_version_id",)),
    ("zr_tool", "ix_zr_tool_owner_user_id", ("owner_user_id",)),
    ("zr_tool", "ix_zr_tool_tenant_id", ("tenant_id",)),
    ("zr_tool_version", "ix_zr_tool_version_status", ("status",)),
    ("zr_tool_version", "ix_zr_tool_version_tenant_id", ("tenant_id",)),
    ("zr_tool_version", "ix_zr_tool_version_tool_id", ("tool_id",)),
)

# (表, 约束名, 目标表, 本地列, 目标列, ondelete)
_FOREIGN_KEYS: tuple[tuple[str, str, str, tuple[str, ...], tuple[str, ...], str | None], ...] = (
    ("zr_agent_version_skill", "fk_zr_agent_version_skill_skill_version", "zr_skill_version", ("skill_version_id",), ("id",), "RESTRICT"),
    ("zr_confirmation", "fk_zr_confirmation_requested_by_user", "zr_user", ("requested_by_user_id",), ("id",), None),
    ("zr_confirmation", "fk_zr_confirmation_confirmed_by_user", "zr_user", ("confirmed_by_user_id",), ("id",), None),
    ("zr_connector", "fk_zr_connector_created_by_user", "zr_user", ("created_by",), ("id",), None),
    ("zr_skill", "fk_zr_skill_owner_user", "zr_user", ("owner_user_id",), ("id",), None),
    ("zr_skill_execution_log", "fk_zr_skill_execution_log_actor_user", "zr_user", ("actor_user_id",), ("id",), None),
    ("zr_tool", "fk_zr_tool_owner_user", "zr_user", ("owner_user_id",), ("id",), None),
)

# 可空引用列：孤儿（引用已删除的用户）置 NULL，保留业务行
_NULLABLE_ORPHANS = (
    ("zr_confirmation", "confirmed_by_user_id"),
    ("zr_skill", "owner_user_id"),
    ("zr_tool", "owner_user_id"),
    ("zr_skill_execution_log", "actor_user_id"),
)

# 非空引用列：孤儿无法自动修复，显式失败并提示，避免加上约束后留下不可见脏数据
_NOT_NULL_ORPHANS = (
    ("zr_confirmation", "requested_by_user_id", "zr_user"),
    ("zr_connector", "created_by", "zr_user"),
    ("zr_agent_version_skill", "skill_version_id", "zr_skill_version"),
)


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def upgrade() -> None:
    conn = op.get_bind()
    # offline 模式（--sql）只有 MockConnection，读不到数据：孤儿校验无法进行，
    # 跳过即可（离线脚本面向空库/审计，不存在历史脏数据）。
    offline = bool(op.get_context().as_sql)

    for table, column in _NULLABLE_ORPHANS:
        op.execute(
            sa.text(
                f"UPDATE {_ident(table)} SET {_ident(column)} = NULL "
                f"WHERE {_ident(column)} IS NOT NULL "
                f"AND {_ident(column)} NOT IN (SELECT id FROM zr_user)"
            )
        )

    if not offline:
        for table, column, referred in _NOT_NULL_ORPHANS:
            count = conn.execute(
                sa.text(
                    f"SELECT COUNT(*) FROM {_ident(table)} "
                    f"WHERE {_ident(column)} NOT IN (SELECT id FROM {_ident(referred)})"
                )
            ).scalar_one()
            if count:
                raise RuntimeError(
                    f"{table}.{column} 存在 {count} 行孤儿引用（{referred} 中已无对应记录），"
                    f"无法安全添加外键；请先修复数据后重跑 alembic upgrade head。"
                )

    for table, name, referred, columns, referred_columns, ondelete in _FOREIGN_KEYS:
        # batch 在 SQLite 上重建表、在 PG 上退化为 ALTER TABLE
        with op.batch_alter_table(table) as batch:
            batch.create_foreign_key(name, referred, list(columns), list(referred_columns), ondelete=ondelete)

    for table, name, columns in _INDEXES:
        # IF NOT EXISTS：兼容曾手工补过索引的部署，避免重复创建导致升级中断
        cols = ", ".join(_ident(column) for column in columns)
        op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {_ident(name)} ON {_ident(table)} ({cols})"))


def downgrade() -> None:
    for _table, name, _columns in reversed(_INDEXES):
        op.execute(sa.text(f"DROP INDEX IF EXISTS {_ident(name)}"))

    for table, name, _referred, _columns, _referred_columns, _ondelete in reversed(_FOREIGN_KEYS):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(name, type_="foreignkey")
