"""审计导出与归档插件（v1.3 M2）。

能力经 Module + /capabilities + 受控 API 暴露；插件只经 PluginSdk 读配置、
读审计（read_audit）与访问存储（object.store Port），不直连内核实现。
"""

from app.builtin_plugins.audit_export_archive.plugin import AuditExportArchivePlugin

__all__ = ["AuditExportArchivePlugin"]
