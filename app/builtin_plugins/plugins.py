"""社区版内置插件集：model.provider / skill.executor / connector / queue.driver。

插件是 Port 实现的唯一装配入口；main.py 不再直接 import 执行器类。
插件只依赖内核契约与配置，装配顺序与依赖见各 manifest。
"""

import logging
from typing import Any

from sqlalchemy import Engine

from app.builtin_plugins.manifest import AssemblyContext, PluginManifest, validate_manifest
from app.kernel.model_gateway import MockModelGateway, OpenAICompatibleGateway
from app.kernel.ports.specs import PORT_SPEC_BY_NAME
from app.kernel.registry.port_registry import PortPriority
from app.kernel.skill_gateway import SkillGateway
from app.builtin_plugins.executors import DbQueryExecutor, HttpExecutor, MdSkillScriptExecutor, McpExecutor, MockExecutor
from app.builtin_plugins.mcp import StdioServerRegistry, make_http_connector
from app.kernel.ports.queue_driver import InMemoryQueueDriver
from app.kernel.db_executor import ConnectorDbExecutor

logger = logging.getLogger("z_torque.plugins")


class ModelProviderPlugin:
    manifest = PluginManifest(
        name="model_provider",
        description="OpenAI 兼容模型网关；未配置时显式降级为 Mock（能力查询可见）",
        provides=("model.provider",),
    )

    def contribute(self, context: AssemblyContext) -> None:
        settings = context.settings
        if settings.model_base_url and settings.model_api_key:
            def factory() -> OpenAICompatibleGateway:
                return OpenAICompatibleGateway(settings.model_base_url, settings.model_api_key.get_secret_value(), default_model=settings.model_name)
            label = f"openai_compatible({settings.model_name})"
        else:
            def factory() -> MockModelGateway:
                return MockModelGateway()
            label = "mock(development fallback)"
            logger.warning("model provider is NOT configured; MockModelGateway is active and will be reported in /capabilities (显式降级，勿用于生产)")
        context.registry.register("model.provider", factory, priority=PortPriority.BUILTIN, label=label, plugin=self.manifest.name)


class SkillExecutorPlugin:
    manifest = PluginManifest(
        name="skill_executors",
        description="SkillGateway 与内置工具执行器（MOCK/HTTP/DB_QUERY/DB_SQL/MD_SCRIPT/MCP 占位）",
        provides=("skill.executor",),
        permissions=("skill.execute", "tool.execute", "db.query"),
    )

    def contribute(self, context: AssemblyContext) -> None:
        gateway = SkillGateway(http_connector_factory=make_http_connector)
        engine: Engine = context.engine
        gateway.register(MockExecutor())
        gateway.register(HttpExecutor())
        gateway.register(DbQueryExecutor(engine))
        gateway.register(ConnectorDbExecutor())
        gateway.register(McpExecutor())
        gateway.register(MdSkillScriptExecutor())
        context.skill_gateway = gateway
        context.registry.register("skill.executor", lambda: gateway, priority=PortPriority.BUILTIN, label="builtin_skill_gateway", plugin=self.manifest.name)


class McpConnectorPlugin:
    manifest = PluginManifest(
        name="mcp_connector",
        description="MCP 外部系统接入（Streamable HTTP / Stdio 传输）",
        provides=("connector",),
        requires=("skill.executor",),
        permissions=("connector.manage",),
    )

    def contribute(self, context: AssemblyContext) -> None:
        registry = StdioServerRegistry()
        context.stdio_registry = registry
        context.skill_gateway.stdio_registry = registry
        service = McpConnectorService(make_http_connector, registry)
        context.registry.register("connector", lambda: service, priority=PortPriority.BUILTIN, label="builtin_mcp(http+stdio)", plugin=self.manifest.name)


class McpConnectorService:
    """connector Port 的内置实现外观：统一暴露 HTTP / Stdio 两类连接器构造。"""

    def __init__(self, http_connector_factory: Any, stdio_registry: StdioServerRegistry) -> None:
        self._http_factory = http_connector_factory
        self._stdio_registry = stdio_registry

    def http_connector(self, endpoint: str, *, allowed_hosts: list[str], timeout_seconds: float = 30, max_response_bytes: int = 1_000_000) -> Any:
        return self._http_factory(endpoint, allowed_hosts=allowed_hosts, timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes)

    def stdio_connector(self, name: str, *, timeout_seconds: float = 30, max_output_bytes: int = 1_000_000) -> Any:
        return self._stdio_registry.connector(name, timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes)


class QueueDriverPlugin:
    manifest = PluginManifest(
        name="queue_driver",
        description="进程内任务队列（社区版单机形态，Outbox 补偿恢复）",
        provides=("queue.driver",),
    )

    def contribute(self, context: AssemblyContext) -> None:
        context.registry.register("queue.driver", InMemoryQueueDriver, priority=PortPriority.BUILTIN, label="builtin_inmemory_queue", plugin=self.manifest.name)


BUILTIN_PLUGINS: tuple[type, ...] = (ModelProviderPlugin, SkillExecutorPlugin, McpConnectorPlugin, QueueDriverPlugin)

# Profile → 插件清单（声明式装配；新增插件在此登记，main.py 不感知）
PROFILE_COMMUNITY: tuple[str, ...] = ("model_provider", "skill_executors", "mcp_connector", "queue_driver")

_KNOWN_PORTS = set(PORT_SPEC_BY_NAME)
for _plugin_cls in BUILTIN_PLUGINS:
    validate_manifest(_plugin_cls.manifest, _KNOWN_PORTS)
