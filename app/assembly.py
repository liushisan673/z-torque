"""装配根（Composition Root）：Profile → Plugin → Port 注册表。

唯一允许导入 app.builtin_plugins 的模块（阶段00 §3.8）。
main.py lifespan 只调用 assemble()，不再直接构造任何 Port 实现。
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine

from app.builtin_plugins.manifest import AssemblyContext, PluginManifest, validate_manifest
from app.builtin_plugins.plugins import BUILTIN_PLUGINS, PROFILE_COMMUNITY
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


def assemble(settings: Any, engine: Engine, profile: tuple[str, ...] = PROFILE_COMMUNITY) -> AssemblyResult:
    """按 Profile 声明加载插件 → 注册 Port → 解析校验。

    任何必需 Port 缺失 / 同优先级歧义 / 插件 requires 未满足都会抛
    AssemblyError，由调用方（lifespan）终止启动。
    """
    registry = PortRegistry()
    context = _AssemblyContext(settings=settings, engine=engine, registry=registry)
    known_ports = set(PORT_SPEC_BY_NAME)
    by_name = {plugin_cls.manifest.name: plugin_cls() for plugin_cls in BUILTIN_PLUGINS}
    unknown = [name for name in profile if name not in by_name]
    if unknown:
        raise AssemblyError(f"profile references unknown plugins: {unknown}")
    selected = [by_name[name] for name in profile]
    for plugin in selected:
        validate_manifest(plugin.manifest, known_ports)
    for plugin in selected:
        plugin.contribute(context)
    registry.resolve()
    for plugin in selected:
        for port_name in plugin.manifest.requires:
            resolution = registry.resolution(port_name)
            if resolution is None or resolution.state != PortState.RESOLVED:
                raise AssemblyError(f"plugin {plugin.manifest.name} requires port {port_name}, which did not resolve")
    if context.skill_gateway is None:
        raise AssemblyError("profile did not provide skill.executor implementation")
    model_gateway = registry.try_get("model.provider")
    if model_gateway is None:
        raise AssemblyError("profile did not provide model.provider implementation")
    stdio_registry = context.stdio_registry
    if stdio_registry is None:
        raise AssemblyError("profile did not provide connector stdio registry")
    return AssemblyResult(registry=registry, skill_gateway=context.skill_gateway, model_gateway=model_gateway, stdio_registry=stdio_registry, plugins=tuple(plugin.manifest for plugin in selected))
