"""标准插件公开契约包（插件唯一允许依赖的 app.* 面之一）。

目的：插件可整目录复制到另一个内核（企业版）而不改一行 import。
本包把插件需要的一切从内核实现里**再导出并冻结**：

| 能力 | 来源 | 说明 |
|---|---|---|
| `PluginManifest` / `PluginSdk` / `validate_manifest` | `app.plugin_sdk.contract` | 契约本体，SDK 即插件唯一入口 |
| `AssemblyError` / `ApiError` | `app.plugin_sdk.errors` | 装配期失败与业务错误（异常身份与内核一致） |
| `AuditQuery` / `AuditRecord` | `app.plugin_sdk.audit` | 审计只读投影类型 |
| `SecurityContext` | `app.plugin_sdk.context` | 身份上下文 |
| `Base` / `TenantModel` | `app.plugin_sdk.db` | 插件自有表的声明基类（插件表仍由内核迁移链建） |

约定：插件源码除本包与 `app.core.*` 公共原语外，不得出现其他 `app.*` import
（`tests/unit/test_plugin_redlines.py` 的 `test_plugins_import_only_public_sdk_surface` 机器校验）。
"""

from app.plugin_sdk.audit import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, AuditQuery, AuditRecord
from app.plugin_sdk.context import SecurityContext
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
from app.plugin_sdk.db import Base, TenantModel, TimestampMixin
from app.plugin_sdk.errors import ApiError, AssemblyError
from app.plugin_sdk.observability import emit_event
from app.plugin_sdk.ports import SandboxDriver

__all__ = [
    "AUDIT_READ_PERMISSION",
    "ENGINE_PERMISSION",
    "MAX_PAGE_SIZE",
    "DEFAULT_PAGE_SIZE",
    "PLUGIN_API_VERSION",
    "PLUGIN_GUARD_KEY",
    "SKILL_GATEWAY_KEY",
    "SUPPORTED_API_VERSIONS",
    "ApiError",
    "AssemblyContext",
    "AssemblyError",
    "AuditQuery",
    "AuditRecord",
    "Base",
    "BuiltinPlugin",
    "SandboxDriver",
    "emit_event",
    "ModuleSpec",
    "PluginManifest",
    "PluginSdk",
    "SecurityContext",
    "TenantModel",
    "TimestampMixin",
    "is_structured_config_schema",
    "project_plugin_config",
    "validate_manifest",
    "validate_module",
    "validate_plugin_config",
]
