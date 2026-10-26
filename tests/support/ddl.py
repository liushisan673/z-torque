"""测试期 DDL 辅助：让"清库"类 fixture 不再绑死 SQLite。

历史用例用 `PRAGMA foreign_keys=OFF` 关闭外键校验做全局清理，在 PostgreSQL 上直接
语法错误（`syntax error at or near "PRAGMA"`），把方言验证变成一片 ERROR。
PG 的等价手段是把会话级 `session_replication_role` 置为 `replica`，效果同为"跳过
外键与触发器校验"，但需要以超级用户（或具备 SET 权限的角色）连接测试库。

注意：这里只服务于**测试库**的清理；生产代码不得依赖该 GUC。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text


def _dialect_name(db: Any) -> str:
    return db.get_bind().dialect.name


def fk_off(db: Any) -> None:
    """关闭外键校验（SQLite: PRAGMA；PostgreSQL: session_replication_role=replica）。"""
    if _dialect_name(db) == "sqlite":
        db.execute(text("PRAGMA foreign_keys=OFF"))
    else:
        db.execute(text("SET session_replication_role = replica"))


def fk_on(db: Any) -> None:
    """恢复外键校验（与 fk_off 成对使用，确保同一事务内复位）。"""
    if _dialect_name(db) == "sqlite":
        db.execute(text("PRAGMA foreign_keys=ON"))
    else:
        db.execute(text("SET session_replication_role = origin"))
