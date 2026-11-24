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
    MdSkillScriptExecutor,
)
from app.builtin_plugins.identity_provider import IdentityProviderPlugin
from app.builtin_plugins.interceptor import InterceptorPlugin
from app.builtin_plugins.knowledge import KnowledgePlugin
from app.builtin_plugins.lock_provider import LockProviderPlugin
from app.builtin_plugins.long_memory import LongMemoryPlugin
from app.builtin_plugins.manifest import (
    ENGINE_PERMISSION,
    PluginManifest,
    PluginSdk,
    validate_manifest,
)
from app.builtin_plugins.mcp import StdioServerRegistry, make_http_connector
from app.builtin_plugins.mcp_lifecycle import MCP_LIFECYCLE_KEY, McpLifecycleService
from app.builtin_plugins.model_gateway import (
    ModelRoutingGateway,
    OpenAICompatibleGateway,
)
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
from app.kernel.model_gateway import ModelGateway
from app.kernel.plugin_guard import GuardedModelGateway, PluginGuardRegistry
from app.kernel.ports.specs import PORT_SPEC_BY_NAME
from app.kernel.registry.port_registry import AssemblyError
from app.kernel.skill_gateway import SkillGateway
from app.plugin_sdk import CapabilityDeclaration, CapabilityLevel
from app.plugin_sdk.contract import SKILL_GATEWAY_KEY

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
        description="模型路由网关：按模型管理配置选择 Provider/Endpoint；全局模型仅作部署方显式兜底，未配置时显式报错（不再降级 Mock）",
        provides=("model.provider",),
        requires=("secret.provider",),
        permissions=(ENGINE_PERMISSION,),
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
        # 全局配置只作为兜底：运行时优先按 request.tenant_id + model 走模型管理。
        # trust_env=False：模型网关永不读取 HTTP(S)_PROXY 环境变量（DeepSeek 拒绝海外出口 IP）。
        import httpx as _httpx

        if settings.model_base_url and api_key:
            default_gateway: Any = OpenAICompatibleGateway(
                settings.model_base_url,
                api_key,
                default_model=settings.model_name,
                client=_httpx.AsyncClient(trust_env=False),
            )
            label = f"model_routing(global={settings.model_name})"
        else:
            # 决策（阶段13）：未配置模型时**不再降级为 MockModelGateway**。
            # 原实现让全新部署“看起来能对话”，实际返回假回复，掩盖了「必须先配模型」这一事实。
            # 现在 default_gateway 为 None，任何对话都会在 ModelRoutingGateway._route 处
            # 抛 AI_MODEL_NOT_AVAILABLE / 在入口处抛 AI_MODEL_NOT_CONFIGURED，由前端引导去配置。
            # Mock 仅由测试通过依赖注入提供（见 tests/conftest.py），生产装配不再创建。
            default_gateway = None
            label = "model_routing(no_global_model)"
            logger.warning(
                "global model is NOT configured; chat/task will fail with AI_MODEL_NOT_CONFIGURED "
                "until a model is configured in 模型管理 (Mock fallback removed by design)"
            )

        router = ModelRoutingGateway(
            session_factory=lambda: sdk.session(),
            secret_provider=secret_provider,
            default_gateway=default_gateway,
        )

        def factory() -> ModelGateway:
            return GuardedModelGateway(router, guard)

        sdk.register_port("model.provider", factory, label=label)


class SkillExecutorPlugin:
    manifest = PluginManifest(
        name="skill_executors",
        description="SkillGateway 与内置工具执行器（HTTP/DB_QUERY/DB_SQL/MD_SCRIPT；MCP 经网关协商执行；MOCK 仅测试 DI）",
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
        gateway.register(HttpExecutor())
        gateway.register(DbQueryExecutor(engine))
        gateway.register(ConnectorDbExecutor())
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
        published_services=(MCP_LIFECYCLE_KEY,),
        capabilities=(
            CapabilityDeclaration("mcp.lifecycle", CapabilityLevel.NARROW_EXTENSION, "窄扩展 connector：分页发现 + 白名单登记"),
        ),
    )

    def contribute(self, sdk: PluginSdk) -> None:
        registry = StdioServerRegistry()
        sdk.attach_stdio_registry(registry)
        service = McpConnectorService(make_http_connector, registry)
        sdk.register_port("connector", lambda: service, label="builtin_mcp(http+stdio)")
        # 领域服务共享给 API 层：不新增内核 Port，装配根会把它转挂 app.state
        lifecycle = McpLifecycleService(sdk.shared(SKILL_GATEWAY_KEY))
        sdk.publish(MCP_LIFECYCLE_KEY, lifecycle)


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


BUILTIN_PLUGINS: tuple[type, ...] = (PolicySourcePlugin, ObservabilitySinkPlugin, InterceptorPlugin, NotifyChannelPlugin, ApprovalProviderPlugin, IdentityProviderPlugin, AuthzProviderPlugin, SecretProviderPlugin, SandboxDriverPlugin, ObjectStorePlugin, WorkingMemoryPlugin, ShortMemoryPlugin, LongMemoryPlugin, KnowledgePlugin, ModelProviderPlugin, RuleBasedModelSelectorPlugin, SkillExecutorPlugin, McpConnectorPlugin, QueueDriverPlugin, LockProviderPlugin, AuditExportArchivePlugin, A2AGatewayPlugin)

_KNOWN_PORTS = set(PORT_SPEC_BY_NAME)
for _plugin_cls in BUILTIN_PLUGINS:
    validate_manifest(_plugin_cls.manifest, _KNOWN_PORTS)
