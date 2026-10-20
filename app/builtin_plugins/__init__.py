"""内置插件包：社区版全部 Port 能力实现的物理载体。

导入约束（阶段00 §3.8）：本包可以导入内核（app.kernel），但
app.kernel / app.api / app.services 禁止导入本包；
唯一允许的加载方是装配根 app/assembly.py。

范围说明（v1.1b）：本原则适用于能力实现（模型适配器/工具执行器/连接器/
队列驱动）；SkillGateway 属内核不变量载体（统一执行入口），保留内核，
仅经本包 SkillExecutorPlugin 注册到 skill.executor Port（装配形式）。
"""

from app.builtin_plugins.db_executors import ConnectorDbExecutor
from app.builtin_plugins.executors import DbQueryExecutor, HttpExecutor, MdSkillScriptExecutor, McpExecutor, MockExecutor
from app.builtin_plugins.manifest import (
    PLUGIN_API_VERSION,
    BuiltinPlugin,
    ModuleSpec,
    PluginManifest,
    PluginSdk,
    validate_manifest,
)
from app.builtin_plugins.mcp import (
    MCP_PROTOCOL_VERSION,
    StdioMcpConnector,
    StdioServerRegistry,
    StdioServerRegistration,
    StreamableHttpMcpConnector,
    make_http_connector,
)
from app.builtin_plugins.model_gateway import MockModelGateway, OpenAICompatibleGateway
from app.builtin_plugins.modules import MODULE_REGISTRY, PROFILE_COMMUNITY
from app.builtin_plugins.plugins import (
    BUILTIN_PLUGINS,
    McpConnectorPlugin,
    ModelProviderPlugin,
    QueueDriverPlugin,
    SkillExecutorPlugin,
)
from app.builtin_plugins.queue_driver import InMemoryQueueDriver

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
