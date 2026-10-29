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
        # 与 a2a_gateway.manifest.requires 对齐：插件确实解析 secret.provider（peer 密钥/
        # mTLS 材料）并向 observability.sink 发事件，模块层声明缺失会让"装配期缺省"漏检
        requires_ports=(
            "skill.executor",
            "identity.provider",
            "authz.provider",
            "secret.provider",
            "observability.sink",
        ),
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


# 选配分隔符：`community+audit_export_archive,a2a` = 社区基础档 + 自选条目
PROFILE_SELECT_SEP = "+"


def _unknown_profile(name: str | None) -> AssemblyError:
    return AssemblyError(f"unknown assembly profile: {name!r} (expect one of {sorted(PROFILE_REGISTRY)})")


def resolve_profile(name: str | None) -> tuple[str, ...]:
    """Profile 名 → 条目元组；未知名字引用即拒绝启动（不静默回退社区版）。

    支持「基础档 + 自选条目」的选配写法（契约 §4 三层结构不变，只是 Profile
    条目追加）：`community+audit_export_archive,a2a` 表示在社区基础上装上
    审计导出归档与 A2A —— 这正是"同一份安装包按需选配插件"的表达方式。

    - 两个内置档（community / enterprise）的展开结果**逐字节不变**；
    - 追加条目允许 Module 名或插件名，存在性由 expand_profile 用**运行时发现**
      （内置 + entry_points）判定，写错即 AssemblyError，不静默忽略；
    - 重复选配基础档已有的插件（无论写 Module 还是插件名）会被**拒绝**而非
      静默忽略：同一插件装配两次会让后注册者顶掉先注册者，且现场会误以为
      "选配生效了"，实际是基础档本来就有。
    """
    key = (name or "community").strip().lower()
    if PROFILE_SELECT_SEP not in key:
        if key not in PROFILE_REGISTRY:
            raise _unknown_profile(name)
        return PROFILE_REGISTRY[key]

    base_name, _, extra_raw = key.partition(PROFILE_SELECT_SEP)
    base_name = base_name.strip()
    if base_name not in PROFILE_REGISTRY:
        raise _unknown_profile(name)
    extras = tuple(item.strip() for item in extra_raw.split(",") if item.strip())
    if not extras:
        raise AssemblyError(f"assembly profile {name!r} has an empty selection list")
    base = PROFILE_REGISTRY[base_name]
    # 展开成插件名后比对：条目可能写 Module（`a2a`）也可能写插件（`a2a_gateway`），
    # 只按字符串去重挡不住"模块已含该插件"的重复装配 —— 重复声明直接拒绝，
    # 而不是静默忽略（否则现场会以为"选配生效了"，实际是基础档本来就有）。
    chosen = {plugin for entry in base for plugin in _expand_entry(entry)}
    selected: list[str] = []
    for entry in extras:
        plugins = _expand_entry(entry)
        duplicated = [plugin for plugin in plugins if plugin in chosen]
        if duplicated:
            raise AssemblyError(
                f"assembly profile {name!r} re-selects {duplicated} already provided by {base_name!r}"
            )
        selected.extend(plugins)
        chosen.update(plugins)
    return (*base, *selected)


def _expand_entry(entry: str) -> tuple[str, ...]:
    """单条 Profile 条目 → 插件名元组（Module 展开；插件名原样保留）。"""
    spec = MODULE_REGISTRY.get(entry)
    return spec.plugins if spec is not None else (entry,)


_KNOWN_PLUGINS = {plugin_cls.manifest.name: plugin_cls for plugin_cls in BUILTIN_PLUGINS}
for _spec in MODULE_REGISTRY.values():
    validate_module(_spec, _KNOWN_PLUGINS)
