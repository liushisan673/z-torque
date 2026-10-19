"""内置插件包：社区版全部 Port 实现的物理载体。

导入约束（阶段00 §3.8）：本包可以导入内核（app.kernel），但
app.kernel / app.api / app.services 禁止导入本包；
唯一允许的加载方是装配根 app/assembly.py。
"""

from app.builtin_plugins.executors import DbQueryExecutor, HttpExecutor, MdSkillScriptExecutor, McpExecutor, MockExecutor
from app.builtin_plugins.manifest import PLUGIN_API_VERSION, AssemblyContext, BuiltinPlugin, PluginManifest, validate_manifest
from app.builtin_plugins.mcp import (
    MCP_PROTOCOL_VERSION,
    StdioMcpConnector,
    StdioServerRegistry,
    StdioServerRegistration,
    StreamableHttpMcpConnector,
    make_http_connector,
)
from app.builtin_plugins.plugins import (
    BUILTIN_PLUGINS,
    PROFILE_COMMUNITY,
    McpConnectorPlugin,
    ModelProviderPlugin,
    QueueDriverPlugin,
    SkillExecutorPlugin,
)

__all__ = [
    "PLUGIN_API_VERSION",
    "AssemblyContext",
    "BuiltinPlugin",
    "PluginManifest",
    "validate_manifest",
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
    "BUILTIN_PLUGINS",
    "PROFILE_COMMUNITY",
    "McpConnectorPlugin",
    "ModelProviderPlugin",
    "QueueDriverPlugin",
    "SkillExecutorPlugin",
]
