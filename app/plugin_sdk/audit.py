"""审计只读契约的公开面（标准插件唯一允许 import 的审计类型来源）。

实现仍在内核（`app.kernel.audit.reader`），此处只做类型/投影的再导出，
使插件不必 `from app.kernel.audit.reader import ...`（企业版只要提供同名契约即可）。

- `AuditQuery`：查询条件（导出/归档的唯一读入口）；
- `AuditRecord`：稳定投影，不暴露 ORM 对象（防插件越权改状态）；
- `DEFAULT_PAGE_SIZE` / `MAX_PAGE_SIZE`：分页上限常量。
"""

from app.kernel.audit.reader import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, AuditQuery, AuditRecord

__all__ = ["DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE", "AuditQuery", "AuditRecord"]
