"""装配根（Composition Root）：Profile → Module → Plugin → Port 注册表。

唯一允许导入 app.builtin_plugins 的模块（阶段00 §3.8）。
main.py lifespan 只调用 assemble()，不再直接构造任何 Port 实现。
"""

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

from sqlalchemy import Engine

from app.builtin_plugins.manifest import PluginManifest, PluginSdk, validate_manifest
from app.builtin_plugins.modules import MODULE_REGISTRY, PROFILE_COMMUNITY
from app.builtin_plugins.plugins import BUILTIN_PLUGINS
from app.kernel.model_gateway import ModelGateway
from app.kernel.ports.specs import PORT_SPEC_BY_NAME
from app.kernel.registry.active import bind_registry
from app.kernel.registry.port_registry import AssemblyError, PortPriority, PortRegistry, PortState
from app.kernel.skill_gateway import SkillGateway
from app.builtin_plugins.mcp import StdioServerRegistry


@dataclass
class _AssemblyContext:
    settings: Any
    engine: Engine
    registry: PortRegistry
    skill_gateway: SkillGateway | None = None
    stdio_registry: StdioServerRegistry | None = None
    objects: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AssemblyResult:
    registry: PortRegistry
    skill_gateway: SkillGateway
    model_gateway: ModelGateway
    stdio_registry: StdioServerRegistry
    plugins: tuple[PluginManifest, ...]


def expand_profile(profile: tuple[str, ...]) -> list[str]:
    """展开 Profile 条目为插件名序列（Module 名展开为其插件；插件名原样保留）。

    顺序语义：按 Profile 声明顺序逐条展开；Module 内插件顺序即其声明顺序。
    """
    known_plugins = {plugin_cls.manifest.name for plugin_cls in BUILTIN_PLUGINS}
    expanded: list[str] = []
    for entry in profile:
        if entry in MODULE_REGISTRY:
            expanded.extend(MODULE_REGISTRY[entry].plugins)
        elif entry in known_plugins:
            expanded.append(entry)
        else:
            raise AssemblyError(f"profile references unknown module or plugin: {entry}")
    return expanded


def _apply_port_overrides(registry: PortRegistry, settings: Any) -> None:
    """显式配置覆盖（契约 §3「显式配置 > 插件 > 内置默认」，P2-15）。

    PORT_OVERRIDES 形如 `memory.working=my_pkg.impl:working_factory`；
    覆盖项以 EXPLICIT 优先级注册，解析时必然优先于插件与内置实现。
    """
    overrides = getattr(settings, "port_override_map", {}) or {}
    for port_name, target in overrides.items():
        module_name, sep, attr = target.partition(":")
        if not sep or not module_name.strip() or not attr.strip():
            raise AssemblyError(f"invalid PORT_OVERRIDES entry for {port_name}: {target!r} (expect module:factory)")
        try:
            module = import_module(module_name.strip())
        except ImportError as exc:
            raise AssemblyError(f"cannot import port override module {module_name!r} for {port_name}") from exc
        factory = getattr(module, attr.strip(), None)
        if not callable(factory):
            raise AssemblyError(f"port override {target!r} for {port_name} is not a callable factory")
        registry.register(port_name, factory, priority=PortPriority.EXPLICIT, label=f"explicit({target})")


def assemble(
    settings: Any,
    engine: Engine,
    profile: tuple[str, ...] = PROFILE_COMMUNITY,
    *,
    extra_plugins: tuple[Any, ...] = (),
) -> AssemblyResult:
    """按 Profile 声明加载模块/插件 → 注册 Port → 解析校验。

    任何必需 Port 缺失 / 同优先级歧义 / 插件 requires 未满足 / Module 级
    requires_ports 未满足都会抛 AssemblyError，由调用方（lifespan）终止启动。
    `extra_plugins` 为第三方插件实例：其 Port 注册默认 PLUGIN 优先级，可覆盖内置实现。
    """
    registry = PortRegistry()
    context = _AssemblyContext(settings=settings, engine=engine, registry=registry)
    known_ports = set(PORT_SPEC_BY_NAME)
    by_name = {plugin_cls.manifest.name: plugin_cls() for plugin_cls in BUILTIN_PLUGINS}
    selected_names = expand_profile(profile)
    selected = [by_name[name] for name in selected_names]
    external = list(extra_plugins)
    for plugin in [*selected, *external]:
        validate_manifest(plugin.manifest, known_ports)
    for plugin, is_builtin in [(item, True) for item in selected] + [(item, False) for item in external]:
        # 插件 SDK（契约 §5.3）：contribute 只经 PluginSdk 读写配置/状态/Port 注册，
        # 插件归属自动标注；装配产出（skill_gateway / stdio_registry）由 SDK 回收
        sdk = PluginSdk(settings=settings, engine=engine, registry=registry, objects=context.objects, manifest=plugin.manifest, builtin=is_builtin)
        plugin.contribute(sdk)
        if sdk.skill_gateway is not None:
            context.skill_gateway = sdk.skill_gateway
        if sdk.stdio_registry is not None:
            context.stdio_registry = sdk.stdio_registry
    # 装配产出回收（P0-1）：stdio 注册表挂在 connector 插件的 SDK 上，而执行期读取它的是
    # skill_executor 插件产出的 SkillGateway。两者分属不同插件实例，必须在此强制收敛，
    # 否则 stdio 类型 MCP 工具执行期恒报 AI_CONNECTOR_UNAVAILABLE。
    if context.stdio_registry is not None:
        if context.skill_gateway is None:
            raise AssemblyError("connector stdio registry requires a skill.executor implementation")
        context.skill_gateway.stdio_registry = context.stdio_registry
    _apply_port_overrides(registry, settings)
    registry.resolve()
    # 内核深层调用点（无 request 上下文）经此取经解析的 Port（P2-14）
    bind_registry(registry)
    # 校验两层级依赖：插件 manifest.requires + Module requires_ports（契约 §4）
    for plugin in [*selected, *external]:
        for port_name in plugin.manifest.requires:
            resolution = registry.resolution(port_name)
            if resolution is None or resolution.state != PortState.RESOLVED:
                raise AssemblyError(f"plugin {plugin.manifest.name} requires port {port_name}, which did not resolve")
    for entry in profile:
        module = MODULE_REGISTRY.get(entry)
        if module is None:
            continue
        for port_name in module.requires_ports:
            resolution = registry.resolution(port_name)
            if resolution is None or resolution.state != PortState.RESOLVED:
                raise AssemblyError(f"module {entry} requires port {port_name}, which did not resolve")
    if context.skill_gateway is None:
        raise AssemblyError("profile did not provide skill.executor implementation")
    model_gateway = registry.try_get("model.provider")
    if model_gateway is None:
        raise AssemblyError("profile did not provide model.provider implementation")
    stdio_registry = context.stdio_registry
    if stdio_registry is None:
        raise AssemblyError("profile did not provide connector stdio registry")
    return AssemblyResult(registry=registry, skill_gateway=context.skill_gateway, model_gateway=model_gateway, stdio_registry=stdio_registry, plugins=tuple(plugin.manifest for plugin in [*selected, *external]))
