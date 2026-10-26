"""Module 装配层（契约 §4 三层结构：Profile → Module → Plugin）。

Module 承载一个相对完整的业务/基础能力域：声明构成插件（顺序即 contribute
顺序）与模块级所需 Port（装配 resolve 后校验，缺失拒绝启动）。
Profile 条目既可以是 Module 名，也可以直接是插件名（细粒度装配/测试用）。
"""

from app.builtin_plugins.manifest import ModuleSpec, validate_module
from app.builtin_plugins.plugins import BUILTIN_PLUGINS
from app.kernel.registry.port_registry import AssemblyError

MODULE_REGISTRY: dict[str, ModuleSpec] = {
    "governance": ModuleSpec(
        name="governance",
        description="横切治理：策略源 / 可观测 / 执行侧拦截链",
        plugins=("policy_source", "observability_sink", "interceptor"),
    ),
    "entry_channels": ModuleSpec(
        name="entry_channels",
        description="入口通道：通知渠道 / 人工审批供给",
        plugins=("notify_channel", "approval_provider"),
    ),
    "identity_access": ModuleSpec(
        name="identity_access",
        description="身份与访问：认证 / 权限裁决",
        plugins=("identity_provider", "authz_provider"),
    ),
    "secret_storage": ModuleSpec(
        name="secret_storage",
        description="密钥加密存储（secret:// 引用）",
        plugins=("secret_provider",),
    ),
    "model_access": ModuleSpec(
        name="model_access",
        description="模型接入与选择（OpenAI 兼容网关 / 规则选择器）",
        plugins=("model_provider", "rule_based_model_selector"),
        requires_ports=("secret.provider",),
    ),
    "memory": ModuleSpec(
        name="memory",
        description="三级记忆：工作 / 短期 / 长期",
        plugins=("working_memory", "short_memory", "long_memory"),
    ),
    "storage": ModuleSpec(
        name="storage",
        description="文件对象存储（本地文件系统）",
        plugins=("object_store",),
    ),
    "execution": ModuleSpec(
        name="execution",
        description="执行底座：沙箱 / 技能网关 / 队列 / 并发锁",
        plugins=("sandbox_driver", "skill_executors", "queue_driver", "lock_provider"),
        requires_ports=("authz.provider",),
    ),
    "connectivity": ModuleSpec(
        name="connectivity",
        description="外部系统连接（MCP HTTP / Stdio）",
        plugins=("mcp_connector",),
        requires_ports=("skill.executor",),
    ),
    "a2a": ModuleSpec(
        name="a2a",
        description="Agent-to-Agent 发现、路由与会话（企业版）",
        plugins=("a2a_gateway",),
        requires_ports=("skill.executor", "identity.provider", "authz.provider"),
    ),
}

# Profile → 模块清单（声明式装配；展开顺序即 contribute 顺序，
# 依赖约束：secret_provider 先于 model_provider；authz/approval/sandbox/interceptor
# 先于 skill_executors；mcp_connector 后于 skill_executors）
PROFILE_COMMUNITY: tuple[str, ...] = (
    "governance",
    "entry_channels",
    "identity_access",
    "secret_storage",
    "model_access",
    "memory",
    "storage",
    "execution",
    "connectivity",
)

# 企业版 Profile：社区集合 + 审计导出归档（governance 域追加插件）+ a2a 模块。
# 显式保留 skill_executors / model_provider / mcp_connector（契约 §2.3），禁止写成
# 「社区集合之外仅两项」这类隐式覆盖。
PROFILE_ENTERPRISE: tuple[str, ...] = (*PROFILE_COMMUNITY, "audit_export_archive", "a2a")

PROFILE_REGISTRY: dict[str, tuple[str, ...]] = {
    "community": PROFILE_COMMUNITY,
    "enterprise": PROFILE_ENTERPRISE,
}


def resolve_profile(name: str | None) -> tuple[str, ...]:
    """Profile 名 → 条目元组；未知名字引用即拒绝启动（不静默回退社区版）。"""
    key = (name or "community").strip().lower()
    if key not in PROFILE_REGISTRY:
        raise AssemblyError(f"unknown assembly profile: {name!r} (expect one of {sorted(PROFILE_REGISTRY)})")
    return PROFILE_REGISTRY[key]


_KNOWN_PLUGINS = {plugin_cls.manifest.name: plugin_cls for plugin_cls in BUILTIN_PLUGINS}
for _spec in MODULE_REGISTRY.values():
    validate_module(_spec, _KNOWN_PLUGINS)
