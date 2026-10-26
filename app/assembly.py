"""装配根（Composition Root）：Profile → Module → Plugin → Port 注册表。

唯一允许导入 app.builtin_plugins 的模块（阶段00 §3.8）。
main.py lifespan 只调用 assemble()，不再直接构造任何 Port 实现。
"""

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

from sqlalchemy import Engine

from app.builtin_plugins.discovery import DiscoveredPlugin, discover_plugins
from app.builtin_plugins.manifest import (
    PluginManifest,
    PluginSdk,
    validate_manifest,
    validate_plugin_config,
)
from app.builtin_plugins.mcp import StdioServerRegistry
from app.builtin_plugins.modules import MODULE_REGISTRY, PROFILE_COMMUNITY, resolve_profile
from app.kernel.model_gateway import ModelGateway
from app.kernel.ports.specs import PORT_SPEC_BY_NAME
from app.kernel.registry.active import bind_registry
from app.kernel.registry.port_registry import AssemblyError, PortPriority, PortRegistry, PortState
from app.kernel.skill_gateway import SkillGateway

__all__ = ["AssemblyResult", "assemble", "expand_profile", "resolve_profile"]


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
    # 装配期共享对象（v1.3 §2.2「能力经 Module + capabilities + 受控 API 暴露」）：
    # 插件经 sdk.publish 发布的领域服务（audit_export / a2a）由装配根回收，
    # main.py 转挂 app.state 供受控 API 使用；未发布时为空字典。
    objects: dict[str, Any] = field(default_factory=dict)
    # 插件实例按装配顺序保留：lifespan 收尾时逆序调用可选的 shutdown(objects)，
    # 释放线程池 / 定时器（插件不得自行注册 signal 或 atexit）
    plugin_instances: tuple[Any, ...] = ()

    def shutdown(self) -> None:
        for plugin in reversed(self.plugin_instances):
            hook = getattr(plugin, "shutdown", None)
            if callable(hook):
                hook(self.objects)


def expand_profile(profile: tuple[str, ...], plugins: tuple[DiscoveredPlugin, ...] | None = None) -> list[str]:
    """展开 Profile 条目为插件名序列（Module 名展开为其插件；插件名原样保留）。

    顺序语义：按 Profile 声明顺序逐条展开；Module 内插件顺序即其声明顺序。
    `plugins` 未显式给出时按运行时发现（内置清单 + entry_points）判定已知插件名。
    """
    discovered = plugins if plugins is not None else discover_plugins()
    known_plugins = {item.name for item in discovered}
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
    plugin_discovery_strict: bool = False,
) -> AssemblyResult:
    """按 Profile 声明加载模块/插件 → 注册 Port → 解析校验。

    任何必需 Port 缺失 / 同优先级歧义 / 插件 requires 未满足 / Module 级
    requires_ports 未满足都会抛 AssemblyError，由调用方（lifespan）终止启动。
    `extra_plugins` 为第三方插件实例：其 Port 注册默认 PLUGIN 优先级，可覆盖内置实现。
    `plugin_discovery_strict`：entry point 加载失败时是否拒绝启动（企业版可开启，
    社区版默认宽松 —— 一个坏的三方包不该让服务起不来）。
    """
    registry = PortRegistry()
    context = _AssemblyContext(settings=settings, engine=engine, registry=registry)
    known_ports = set(PORT_SPEC_BY_NAME)
    # 插件发现（阶段 2）：内置元组兜底在前，entry_points 追加在后。
    # 只实例化 Profile 实际选中的插件（未选中的不再构造，语义等价）。
    discovered = discover_plugins(strict=plugin_discovery_strict)
    by_name = {item.name: item for item in discovered}
    selected_names = expand_profile(profile, discovered)
    selected = [by_name[name].cls() for name in selected_names]
    selected_builtin = [by_name[name].builtin for name in selected_names]
    external = list(extra_plugins)
    # 重复插件名必须显式拒绝：静默后写会覆盖先写（Port 注册顺序决定行为），
    # 这类"装上了但被顶掉"的故障在生产现场极难定位。
    external_names = [item.manifest.name for item in external]
    duplicates = sorted({name for name in external_names if external_names.count(name) > 1})
    if duplicates:
        raise AssemblyError(f"duplicate plugin names in extra_plugins: {duplicates}")
    collision = sorted(set(external_names) & set(selected_names))
    if collision:
        raise AssemblyError(
            f"plugins {collision} are provided both by the profile and extra_plugins; "
            "remove one side instead of registering the same plugin twice"
        )
    for plugin in [*selected, *external]:
        validate_manifest(plugin.manifest, known_ports)
        # 配置形状校验（阶段 2）：结构化 config_schema 按 Settings 投影后校验，
        # 类型/枚举/必填不符即拒绝启动；历史描述性 schema 不参与校验（行为不变）。
        validate_plugin_config(plugin.manifest, settings)
    for plugin, is_builtin in list(zip(selected, selected_builtin)) + [(item, False) for item in external]:
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
    return AssemblyResult(
        registry=registry,
        skill_gateway=context.skill_gateway,
        model_gateway=model_gateway,
        stdio_registry=stdio_registry,
        plugins=tuple(plugin.manifest for plugin in [*selected, *external]),
        objects=dict(context.objects),
        plugin_instances=tuple([*selected, *external]),
    )
