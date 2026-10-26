"""社区版内置插件集：21 Port 全量收口的社区版实现。

插件是 Port 实现的唯一装配入口；main.py 不再直接 import 执行器类。
插件只依赖内核契约与配置，装配顺序与依赖见各 manifest。
P2 收口（12 Port）：notify.channel / approval.provider / sandbox.driver /
memory.working / memory.short / memory.long / object.store / interceptor /
observability.sink / policy.source（vector.store / knowledge.provider 契约已定义，
社区版无内置实现 → capabilities 显式 UNAVAILABLE）。
"""

import logging
from typing import Any

from pydantic import SecretStr
from sqlalchemy import Engine

from app.builtin_plugins.a2a_gateway import A2AGatewayPlugin
from app.builtin_plugins.approval_provider import ApprovalProviderPlugin
from app.builtin_plugins.audit_export_archive import AuditExportArchivePlugin
from app.builtin_plugins.authz_provider import AuthzProviderPlugin
from app.builtin_plugins.db_executors import ConnectorDbExecutor
from app.builtin_plugins.executors import (
    DbQueryExecutor,
    HttpExecutor,
    McpExecutor,
    MdSkillScriptExecutor,
    MockExecutor,
)
from app.builtin_plugins.identity_provider import IdentityProviderPlugin
from app.builtin_plugins.interceptor import InterceptorPlugin
from app.builtin_plugins.lock_provider import LockProviderPlugin
from app.builtin_plugins.long_memory import LongMemoryPlugin
from app.builtin_plugins.manifest import PluginManifest, PluginSdk, validate_manifest
from app.builtin_plugins.mcp import StdioServerRegistry, make_http_connector
from app.builtin_plugins.model_gateway import MockModelGateway, OpenAICompatibleGateway
from app.builtin_plugins.model_selector import RuleBasedModelSelectorPlugin
from app.builtin_plugins.notify_channel import NotifyChannelPlugin
from app.builtin_plugins.object_store import ObjectStorePlugin
from app.builtin_plugins.observability_sink import ObservabilitySinkPlugin
from app.builtin_plugins.policy_source import PolicySourcePlugin
from app.builtin_plugins.queue_driver import InMemoryQueueDriver
from app.builtin_plugins.sandbox_driver import SandboxDriverPlugin
from app.builtin_plugins.secret_provider import SecretProviderPlugin
from app.builtin_plugins.short_memory import ShortMemoryPlugin
from app.builtin_plugins.working_memory import WorkingMemoryPlugin
from app.kernel.plugin_guard import GuardedModelGateway, PluginGuardRegistry
from app.kernel.ports.specs import PORT_SPEC_BY_NAME
from app.kernel.registry.port_registry import AssemblyError
from app.kernel.skill_gateway import SkillGateway

logger = logging.getLogger("z_torque.plugins")


def _resolve_api_key(secret_provider, raw_key: SecretStr | str | None) -> str | None:
    """G3：API Key 支持 secret:// 引用（经 secret.provider 解密）或明文 env 配置（向后兼容）。"""
    if raw_key is None:
        return None
    key = raw_key.get_secret_value() if isinstance(raw_key, SecretStr) else str(raw_key)
    if key.startswith("secret://"):
        return secret_provider.decrypt(key.removeprefix("secret://"))
    return key


class ModelProviderPlugin:
    manifest = PluginManifest(
        name="model_provider",
        description="OpenAI 兼容模型网关；未配置时显式降级为 Mock（能力查询可见）",
        provides=("model.provider",),
        requires=("secret.provider",),
    )

    def contribute(self, sdk: PluginSdk) -> None:
        settings = sdk.settings
        # contribute 阶段 Port 尚未 resolve，经装配上下文共享对象取 secret.provider 实例
        #（由 SecretProviderPlugin 写入；缺失即依赖未满足，与 manifest.requires 语义一致）
        secret_provider = sdk.shared("secret.provider")
        if secret_provider is None:
            raise AssemblyError("plugin model_provider requires secret.provider port; load secret_provider first")
        api_key = _resolve_api_key(secret_provider, settings.model_api_key)
        # 插件级熔断与配额（契约 §5.5）：模型网关统一经 GuardedModelGateway 守卫
        guard = PluginGuardRegistry.from_settings(settings)
        if settings.model_base_url and api_key:
            def factory() -> OpenAICompatibleGateway:
                return GuardedModelGateway(OpenAICompatibleGateway(settings.model_base_url, api_key, default_model=settings.model_name), guard)
            label = f"openai_compatible({settings.model_name})"
        else:
            def factory() -> MockModelGateway:
                return GuardedModelGateway(MockModelGateway(), guard)
            label = "mock(development fallback)"
            logger.warning("model provider is NOT configured; MockModelGateway is active and will be reported in /capabilities (显式降级，勿用于生产)")
        sdk.register_port("model.provider", factory, label=label)


class SkillExecutorPlugin:
    manifest = PluginManifest(
        name="skill_executors",
        description="SkillGateway 与内置工具执行器（MOCK/HTTP/DB_QUERY/DB_SQL/MD_SCRIPT/MCP 占位）",
        provides=("skill.executor",),
        requires=("authz.provider",),
        permissions=("skill.execute", "tool.execute", "db.query"),
    )

    def contribute(self, sdk: PluginSdk) -> None:
        # authz / approval / sandbox / interceptor 由各自插件在 contribute 阶段发布
        # 到装配上下文（Port 实例化要等 resolve()，与 model_provider/secret.provider 同模式）；
        # PEP 裁决点位置不变（SkillGateway 执行前检查），仅策略来源经 Port 抽象（P1-3）；
        # 审计发射 / 审批供给 / 沙箱执行三 Port 注入为 P2 收口
        authz_provider = sdk.shared("authz.provider")
        if authz_provider is None:
            raise AssemblyError("plugin skill_executors requires authz.provider port; load authz_provider first")
        gateway = SkillGateway(
            authz_provider,
            http_connector_factory=make_http_connector,
            approval_provider=sdk.shared("approval.provider"),
            sandbox_driver=sdk.shared("sandbox.driver"),
            interceptor=sdk.shared("interceptor"),
            secret_provider=sdk.shared("secret.provider"),
        )
        engine: Engine = sdk.engine
        gateway.register(MockExecutor())
        gateway.register(HttpExecutor())
        gateway.register(DbQueryExecutor(engine))
        gateway.register(ConnectorDbExecutor())
        gateway.register(McpExecutor())
        gateway.register(MdSkillScriptExecutor())
        sdk.expose_skill_gateway(gateway)
        sdk.register_port("skill.executor", lambda: gateway, label="builtin_skill_gateway")


class McpConnectorPlugin:
    manifest = PluginManifest(
        name="mcp_connector",
        description="MCP 外部系统接入（Streamable HTTP / Stdio 传输）",
        provides=("connector",),
        requires=("skill.executor",),
        permissions=("connector.manage",),
    )

    def contribute(self, sdk: PluginSdk) -> None:
        registry = StdioServerRegistry()
        sdk.attach_stdio_registry(registry)
        service = McpConnectorService(make_http_connector, registry)
        sdk.register_port("connector", lambda: service, label="builtin_mcp(http+stdio)")


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

    def contribute(self, sdk: PluginSdk) -> None:
        sdk.register_port("queue.driver", InMemoryQueueDriver, label="builtin_inmemory_queue")


BUILTIN_PLUGINS: tuple[type, ...] = (PolicySourcePlugin, ObservabilitySinkPlugin, InterceptorPlugin, NotifyChannelPlugin, ApprovalProviderPlugin, IdentityProviderPlugin, AuthzProviderPlugin, SecretProviderPlugin, SandboxDriverPlugin, ObjectStorePlugin, WorkingMemoryPlugin, ShortMemoryPlugin, LongMemoryPlugin, ModelProviderPlugin, RuleBasedModelSelectorPlugin, SkillExecutorPlugin, McpConnectorPlugin, QueueDriverPlugin, LockProviderPlugin, AuditExportArchivePlugin, A2AGatewayPlugin)

_KNOWN_PORTS = set(PORT_SPEC_BY_NAME)
for _plugin_cls in BUILTIN_PLUGINS:
    validate_manifest(_plugin_cls.manifest, _KNOWN_PORTS)
