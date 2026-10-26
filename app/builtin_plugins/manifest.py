"""内置插件契约的兼容入口（实现已迁至 `app/plugin_sdk`）。

历史上 21 个内置插件都 `from app.builtin_plugins.manifest import ...`；
标准插件（sandbox_driver / audit_export_archive / a2a_gateway）改用公开契约包
`app.plugin_sdk`，本模块保留为再导出 shim，避免一次性改动全部插件带来的风险。

约定：新代码请 import `app.plugin_sdk`；本 shim 只做转发，不承载实现。
"""

from app.plugin_sdk.contract import (
    AUDIT_READ_PERMISSION,
    ENGINE_PERMISSION,
    PLUGIN_API_VERSION,
    PLUGIN_GUARD_KEY,
    SKILL_GATEWAY_KEY,
    SUPPORTED_API_VERSIONS,
    AssemblyContext,
    BuiltinPlugin,
    ModuleSpec,
    PluginManifest,
    PluginSdk,
    is_structured_config_schema,
    project_plugin_config,
    validate_manifest,
    validate_module,
    validate_plugin_config,
)

__all__ = [
    "AUDIT_READ_PERMISSION",
    "ENGINE_PERMISSION",
    "PLUGIN_API_VERSION",
    "PLUGIN_GUARD_KEY",
    "SKILL_GATEWAY_KEY",
    "SUPPORTED_API_VERSIONS",
    "AssemblyContext",
    "BuiltinPlugin",
    "ModuleSpec",
    "PluginManifest",
    "PluginSdk",
    "is_structured_config_schema",
    "project_plugin_config",
    "validate_manifest",
    "validate_module",
    "validate_plugin_config",
]
