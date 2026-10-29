"""a2a_gateway 插件入口（v1.3 M3）。

装配期：读取并校验 A2A 配置 → 构造 identity / discovery / peers / gateway →
经 `sdk.publish(SERVICE_A2A_GATEWAY, gateway)` 交装配根，main.py 转挂 app.state。
`A2A_ENABLED=false` 时仍装配（路由常注册，handler 返回 `AI_A2A_DISABLED` 503），
避免"未装配"与"已关闭"两种语义被前端混淆。
"""

from __future__ import annotations

from typing import Any

from app.builtin_plugins.a2a_gateway.config import A2AConfig
from app.builtin_plugins.a2a_gateway.discovery import AgentDiscovery
from app.builtin_plugins.a2a_gateway.federated import FederatedPrincipalRegistry
from app.builtin_plugins.a2a_gateway.gateway import A2AGateway
from app.builtin_plugins.a2a_gateway.identity import InboundIdentity
from app.builtin_plugins.a2a_gateway.peers import PeerRegistry
from app.plugin_sdk import ENGINE_PERMISSION, SKILL_GATEWAY_KEY, AssemblyError, PluginManifest

SERVICE_A2A_GATEWAY = "a2a_gateway"
NETWORK_OUTBOUND_PERMISSION = "network.outbound"


class A2AGatewayPlugin:
    manifest = PluginManifest(
        name="a2a_gateway",
        description="Agent-to-Agent 发现、路由与会话；独立于单层父子 Task",
        provides=(),
        requires=(
            "skill.executor",
            "identity.provider",
            "authz.provider",
            "secret.provider",
            "observability.sink",
        ),
        # network.outbound：每次出站前 require_permission 自证；db.query：仅 identity/discovery
        permissions=(NETWORK_OUTBOUND_PERMISSION, ENGINE_PERMISSION),
        # 结构化 JSON Schema：装配期按 Settings 投影校验（类型/枚举/必填）；
        # 跨字段语义（remote_peers 非空时白名单不得为空、base_url 必须 https）在 A2AConfig.validate()。
        config_schema={
            "type": "object",
            "properties": {
                "a2a_enabled": {"type": "boolean", "default": False, "description": "总开关"},
                "a2a_local_discovery": {
                    "type": "boolean",
                    "default": True,
                    "description": "对外暴露本地 Agent 只读投影",
                },
                "a2a_remote_peers": {
                    "type": "string",
                    "default": "[]",
                    "description": "JSON 数组字符串：[{id, base_url, auth_secret_name, timeout_seconds, tenant_id, inbound_federation, client_cert_secret_name, client_key_secret_name, ca_bundle_secret_name}]",
                },
                "a2a_allowed_peers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "出站白名单；空 = 全部拒绝",
                },
                "a2a_max_depth": {
                    "type": "integer",
                    "default": 2,
                    "description": "基于 X-A2A-Depth 的硬限",
                },
                "a2a_max_fanout": {
                    "type": "integer",
                    "default": 5,
                    "description": "单次扇出并发上限",
                },
                "a2a_default_timeout_seconds": {
                    "type": "number",
                    "default": 120.0,
                    "description": "出站超时（秒）",
                },
                "a2a_identity_propagation": {
                    "type": "string",
                    "enum": ["none", "user_id_header", "full_context_token"],
                    "default": "user_id_header",
                    "description": "身份传播方式（仅出站；入站一律重新认证）",
                },
                "a2a_max_response_bytes": {
                    "type": "integer",
                    "default": 1000000,
                    "description": "出站响应体上限，超限即失败",
                },
                "a2a_max_request_bytes": {
                    "type": "integer",
                    "default": 1000000,
                    "description": "入站请求体上限，超限按 AI_A2A_DENIED 拒绝（与信封结构错误同码）",
                },
                "a2a_circuit_failure_threshold": {
                    "type": "integer",
                    "default": 5,
                    "description": "对端熔断失败阈值",
                },
                "a2a_circuit_cooldown_seconds": {
                    "type": "number",
                    "default": 30.0,
                    "description": "对端熔断冷却时间（秒）",
                },
                "a2a_federation_enabled": {
                    "type": "boolean",
                    "default": False,
                    "description": "联邦入站总开关：开启后 inbound_federation peer 可用共享密钥（service account）入站；入站真 mTLS 属部署层职责",
                },
            },
            "additionalProperties": False,
        },
    )

    def contribute(self, sdk) -> None:
        config = self._load_config(sdk)
        # 熔断/配额实例由内核经 SDK 注入（全装配期唯一），插件不自行构造
        guard = sdk.guard
        gateway = sdk.shared(SKILL_GATEWAY_KEY)
        if gateway is None:
            # skill.executor 已由 Module requires_ports 保证存在；此处兜底防装配顺序漂移
            raise AssemblyError("plugin a2a_gateway requires skill.executor implementation in assembly context")
        # 联邦入站（方案 B）：开关关闭时返回 None（零行为变化）；开启时装配期预解密
        # 共享密钥并校验租户存在性，失败即 AssemblyError
        federated = FederatedPrincipalRegistry.from_config(sdk, config)
        identity = InboundIdentity(sdk, federated=federated)
        discovery = AgentDiscovery(sdk)
        peers = PeerRegistry(sdk, config, guard=guard)
        a2a_gateway = A2AGateway(
            sdk,
            config,
            identity=identity,
            discovery=discovery,
            peers=peers,
            skill_gateway=gateway,
            guard=guard,
        )
        sdk.publish(SERVICE_A2A_GATEWAY, a2a_gateway)

    @staticmethod
    def _load_config(sdk: Any) -> A2AConfig:
        try:
            config = A2AConfig.from_settings(sdk.settings)
            config.validate()
        except ValueError as exc:
            raise AssemblyError(f"plugin a2a_gateway has invalid configuration: {exc}") from exc
        return config
