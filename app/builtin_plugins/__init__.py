"""内置插件包：社区版全部 Port 能力实现的物理载体。

导入约束（阶段00 §3.8）：本包可以导入内核（app.kernel），但
app.kernel / app.api / app.services 禁止导入本包；
唯一允许的加载方是装配根 app/assembly.py。

范围说明（v1.1b）：本原则适用于能力实现（模型适配器/工具执行器/连接器/
队列驱动）；SkillGateway 属内核不变量载体（统一执行入口），保留内核，
仅经本包 SkillExecutorPlugin 注册到 skill.executor Port（装配形式）。

**本__init__ 不得聚合导入子模块**（2026-10-26，标准插件可移植性）：
一旦在这里 `import` 全部插件，`import app.builtin_plugins.<one_plugin>` 就会
连带拉起 19 个插件 + app.models + app.services，插件的"可整目录拷走"名存实亡。
因此改为 PEP 562 惰性导出：`from app.builtin_plugins import X` 仍然可用（首次访问时解析），
但 import 任一子模块不再触发全量加载。
"""

from __future__ import annotations

from typing import Any

from app.builtin_plugins.manifest import (
    PLUGIN_API_VERSION,
    BuiltinPlugin,
    ModuleSpec,
    PluginManifest,
    PluginSdk,
    validate_manifest,
)

# 名字 -> (模块, 属性)；与历史 __all__ 完全一一对应，惰性解析保持向后兼容。
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "ConnectorDbExecutor": ("app.builtin_plugins.db_executors", "ConnectorDbExecutor"),
    "DbQueryExecutor": ("app.builtin_plugins.executors", "DbQueryExecutor"),
    "HttpExecutor": ("app.builtin_plugins.executors", "HttpExecutor"),
    "MdSkillScriptExecutor": ("app.builtin_plugins.executors", "MdSkillScriptExecutor"),
    "McpExecutor": ("app.builtin_plugins.executors", "McpExecutor"),
    "MockExecutor": ("app.builtin_plugins.executors", "MockExecutor"),
    "MCP_PROTOCOL_VERSION": ("app.builtin_plugins.mcp", "MCP_PROTOCOL_VERSION"),
    "StdioMcpConnector": ("app.builtin_plugins.mcp", "StdioMcpConnector"),
    "StdioServerRegistry": ("app.builtin_plugins.mcp", "StdioServerRegistry"),
    "StdioServerRegistration": ("app.builtin_plugins.mcp", "StdioServerRegistration"),
    "StreamableHttpMcpConnector": ("app.builtin_plugins.mcp", "StreamableHttpMcpConnector"),
    "make_http_connector": ("app.builtin_plugins.mcp", "make_http_connector"),
    "MockModelGateway": ("app.builtin_plugins.model_gateway", "MockModelGateway"),
    "OpenAICompatibleGateway": ("app.builtin_plugins.model_gateway", "OpenAICompatibleGateway"),
    "MODULE_REGISTRY": ("app.builtin_plugins.modules", "MODULE_REGISTRY"),
    "PROFILE_COMMUNITY": ("app.builtin_plugins.modules", "PROFILE_COMMUNITY"),
    "InMemoryQueueDriver": ("app.builtin_plugins.queue_driver", "InMemoryQueueDriver"),
    "BUILTIN_PLUGINS": ("app.builtin_plugins.plugins", "BUILTIN_PLUGINS"),
    "McpConnectorPlugin": ("app.builtin_plugins.plugins", "McpConnectorPlugin"),
    "ModelProviderPlugin": ("app.builtin_plugins.plugins", "ModelProviderPlugin"),
    "QueueDriverPlugin": ("app.builtin_plugins.plugins", "QueueDriverPlugin"),
    "SkillExecutorPlugin": ("app.builtin_plugins.plugins", "SkillExecutorPlugin"),
}

_EAGER = frozenset({"PLUGIN_API_VERSION", "BuiltinPlugin", "ModuleSpec", "PluginManifest", "PluginSdk", "validate_manifest"})


def __getattr__(name: str) -> Any:
    if name in _EAGER:
        return globals()[name]
    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    import importlib

    value = getattr(importlib.import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "PLUGIN_API_VERSION",
    "BuiltinPlugin",
    "PluginManifest",
    "PluginSdk",
    "ModuleSpec",
    "validate_manifest",
    "ConnectorDbExecutor",
    "DbQueryExecutor",
    "HttpExecutor",
    "MdSkillScriptExecutor",
    "McpExecutor",
    "MockExecutor",
    "MCP_PROTOCOL_VERSION",
    "StdioMcpConnector",
    "StdioServerRegistry",
    "StdioServerRegistration",
    "StreamableHttpMcpConnector",
    "make_http_connector",
    "MockModelGateway",
    "OpenAICompatibleGateway",
    "InMemoryQueueDriver",
    "BUILTIN_PLUGINS",
    "MODULE_REGISTRY",
    "PROFILE_COMMUNITY",
    "McpConnectorPlugin",
    "ModelProviderPlugin",
    "QueueDriverPlugin",
    "SkillExecutorPlugin",
]
