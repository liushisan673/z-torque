"""插件自有表的声明基类（再导出 `app.db.base`）。

方案 b（插件表归属）：插件私有表的**模型定义与迁移文件**都跟随插件目录，
但 DDL 仍由内核统一 Alembic 链执行，插件运行时没有任何 DDL 权限。
插件因此只需要声明基类，不需要 `from app.db.base import ...`。
"""

from app.db.base import GUID, Base, TenantModel, TimestampMixin, UUIDModel

__all__ = ["GUID", "Base", "TenantModel", "TimestampMixin", "UUIDModel"]
