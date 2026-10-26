"""align datetime columns with the models' timezone semantics

在全新 PostgreSQL 库上跑完整迁移链后，`compare_metadata` 仍会报 28 处时间戳差异：

* 27 列在迁移里建成了 ``TIMESTAMP WITHOUT TIME ZONE``，模型声明的是
  ``DateTime(timezone=True)``。运行时代码写入的是带时区的 UTC（``utc_now()``），
  列却是 naive，PG 会静默丢弃偏移——数据"看着像 UTC"但语义已丢失。
* 3 列（``zr_agent_version.published_at``、``zr_conversation.last_message_at``、
  ``zr_conversation.archived_at``）迁移建成 timestamptz，模型没声明
  ``timezone=True``：PG 读出来是 aware，代码按 naive 处理会抛 TypeError。
  （模型已在本提交中一并修正，这里只补 DDL。）
* ``zr_task_checkpoint`` 上由迁移的 ``unique=True`` 生成了 ``..._task_id_key``
  唯一约束，而模型走的是「唯一索引」路径，两者不等价，多余约束需删除。

存量库大多是 timestamptz（历史 batch 重建把它带过来了），所以对它们这些
ALTER 是幂等的；对仍是 naive 的库，按「库里存的是 UTC」解释时区后转换。

SQLite 不需要处理：DATETIME 没有时区概念，两方言比较视为等价。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3d6f9a2c5e8"
down_revision: str | Sequence[str] | None = "f6b7c8d9e0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TIMESTAMP_COLUMNS: dict[str, tuple[str, ...]] = {
    # 存量库这几列是 naive（模型原本也没声明 timezone），干净迁移库却是 timestamptz；
    # 两种来源都要收敛到 timestamptz，所以一并列入（对已是 timestamptz 的库是幂等的）。
    "zr_agent_version": ("published_at",),
    "zr_conversation": ("last_message_at", "archived_at"),
    "zr_confirmation": ("created_at", "updated_at"),
    "zr_connector": ("created_at", "updated_at"),
    "zr_connector_tool": ("created_at", "updated_at"),
    "zr_file": ("created_at", "updated_at"),
    "zr_memory_short_term": ("created_at", "updated_at"),
    "zr_skill": ("created_at", "updated_at"),
    "zr_skill_execution_log": ("created_at", "updated_at"),
    "zr_skill_version": ("created_at", "updated_at"),
    "zr_skill_version_tool": ("created_at", "updated_at"),
    "zr_task_checkpoint": ("resumed_at", "created_at", "updated_at"),
    "zr_tool": ("created_at", "updated_at"),
    "zr_tool_version": ("created_at", "updated_at"),
}

_REDUNDANT_UNIQUE = ("zr_task_checkpoint", "zr_task_checkpoint_task_id_key")


def _is_postgresql() -> bool:
    return op.get_context().dialect.name == "postgresql"


def _alter_column_type(table: str, column: str, *, with_timezone: bool) -> None:
    target = "TIMESTAMP WITH TIME ZONE" if with_timezone else "TIMESTAMP WITHOUT TIME ZONE"
    op.execute(
        sa.text(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE {target} '
            f'USING "{column}" AT TIME ZONE \'UTC\''
        )
    )


def upgrade() -> None:
    if not _is_postgresql():
        return
    for table, columns in _TIMESTAMP_COLUMNS.items():
        for column in columns:
            _alter_column_type(table, column, with_timezone=True)
    table, constraint = _REDUNDANT_UNIQUE
    op.execute(sa.text(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{constraint}"'))


def downgrade() -> None:
    if not _is_postgresql():
        return
    for table, columns in _TIMESTAMP_COLUMNS.items():
        for column in columns:
            _alter_column_type(table, column, with_timezone=False)
    # 多余的唯一约束不回建：它与模型定义不一致，回建等于把缺陷放回去。
