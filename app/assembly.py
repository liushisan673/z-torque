"""装配根（Composition Root）：Profile → Module → Plugin → Port 注册表。

唯一允许导入 app.builtin_plugins 的模块（阶段00 §3.8）。
main.py lifespan 只调用 assemble()，不再直接构造任何 Port 实现。
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine

from app.builtin_plugins.manifest import PluginManifest, PluginSdk, validate_manifest
from app.builtin_plugins.modules import MODULE_REGISTRY, PROFILE_COMMUNITY
from app.builtin_plugins.plugins import BUILTIN_PLUGINS
from app.kernel.model_gateway import ModelGateway
from app.kernel.ports.specs import PORT_SPEC_BY_NAME
from app.kernel.registry.port_registry import AssemblyError, PortRegistry, PortState
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


def assemble(settings: Any, engine: Engine, profile: tuple[str, ...] = PROFILE_COMMUNITY) -> AssemblyResult:
    """按 Profile 声明加载模块/插件 → 注册 Port → 解析校验。

    任何必需 Port 缺失 / 同优先级歧义 / 插件 requires 未满足 / Module 级
    requires_ports 未满足都会抛 AssemblyError，由调用方（lifespan）终止启动。
    """
    registry = PortRegistry()
    context = _AssemblyContext(settings=settings, engine=engine, registry=registry)
    known_ports = set(PORT_SPEC_BY_NAME)
    by_name = {plugin_cls.manifest.name: plugin_cls() for plugin_cls in BUILTIN_PLUGINS}
    selected_names = expand_profile(profile)
    selected = [by_name[name] for name in selected_names]
    for plugin in selected:
        validate_manifest(plugin.manifest, known_ports)
    for plugin in selected:
        # 插件 SDK（契约 §5.3）：contribute 只经 PluginSdk 读写配置/状态/Port 注册，
        # 插件归属自动标注；装配产出（skill_gateway / stdio_registry）由 SDK 回收
        sdk = PluginSdk(settings=settings, engine=engine, registry=registry, objects=context.objects, manifest=plugin.manifest)
        plugin.contribute(sdk)
        if sdk.skill_gateway is not None:
            context.skill_gateway = sdk.skill_gateway
        if sdk.stdio_registry is not None:
            context.stdio_registry = sdk.stdio_registry
    registry.resolve()
    # 校验两层级依赖：插件 manifest.requires + Module requires_ports（契约 §4）
    for plugin in selected:
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
    return AssemblyResult(registry=registry, skill_gateway=context.skill_gateway, model_gateway=model_gateway, stdio_registry=stdio_registry, plugins=tuple(plugin.manifest for plugin in selected))
