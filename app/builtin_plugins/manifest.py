"""内置插件契约：manifest / 插件 SDK / Module（阶段00 §3.6 / 契约 §5 五条纪律）。

- manifest 唯一入口：声明 apiVersion / provides / requires / permissions。
- 插件只实现内核已定义的 Port（registry 对未知 Port 直接拒绝）。
- **插件 SDK**（契约 §5.3「只经内核 SDK 读写状态」）：插件经 PluginSdk 读取配置、
  注册 Port、共享装配期对象、受控访问数据库——不直接持有装配上下文内部结构；
  裸 engine 仅对声明 db.query 权限的插件开放（契约 §5.4 权限声明式）。
- Module（契约 §4 三层结构）：Module 声明构成插件与所需 Port，Profile 按模块装配。
- 内核（app/kernel、app/api、app/services）禁止导入本包；仅装配根
  app/assembly.py 允许加载插件。
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.kernel.registry.port_registry import PortPriority

PLUGIN_API_VERSION = "zsoftym.ai/plugin.v1"

# 访问裸数据库引擎所需的权限（契约 §5.4：未声明即拒绝）
ENGINE_PERMISSION = "db.query"


@dataclass(frozen=True)
class PluginManifest:
    name: str
    description: str
    provides: tuple[str, ...]
    requires: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    api_version: str = PLUGIN_API_VERSION
    version: str = "0.1.0"
    config_schema: dict[str, Any] | None = None


class PluginSdk:
    """插件 SDK：插件 contribute(sdk) 的唯一授权接口（契约 §5.3/§5.4）。

    - settings：配置只读引用（插件不得修改运行时配置）；
    - register_port：Port 注册（插件归属自动标注，防伪造）；
    - publish / shared：装配期对象共享（替代裸 objects 字典直改）；
    - engine：裸数据库引擎，仅声明 db.query 权限的插件可访问（查询型执行器）；
    - expose_skill_gateway / attach_stdio_registry：装配产出物挂载。
    """

    def __init__(self, *, settings: Any = None, engine: Any = None, registry: Any = None, objects: dict[str, Any] | None = None, manifest: PluginManifest | None = None) -> None:
        self._settings = settings
        self._engine = engine
        self._registry = registry
        self._objects = objects if objects is not None else {}
        # 直构/测试场景未传 manifest 时使用宽松占位（生产装配始终传真实 manifest）
        self._manifest = manifest or PluginManifest(name="adhoc", description="", provides=(), permissions=(ENGINE_PERMISSION,))
        self._skill_gateway: Any = None
        self._stdio_registry: Any = None

    @classmethod
    def of(cls, plugin: Any, *, settings: Any = None, engine: Any = None, registry: Any = None, objects: dict[str, Any] | None = None) -> "PluginSdk":
        """按插件 manifest 构造 SDK（测试/直构便利；生产装配由 assembly 逐插件构造）。"""
        return cls(settings=settings, engine=engine, registry=registry, objects=objects, manifest=plugin.manifest)

    @property
    def settings(self) -> Any:
        return self._settings

    @property
    def registry(self) -> Any:
        """Port 注册表（只读引用；注册经 register_port 以保证插件归属标注）。"""
        return self._registry

    @property
    def engine(self) -> Any:
        if ENGINE_PERMISSION not in self._manifest.permissions:
            raise PermissionError(f"plugin {self._manifest.name} lacks '{ENGINE_PERMISSION}' permission; engine access is denied (契约 §5.4)")
        return self._engine

    def register_port(self, port_name: str, factory: Any, *, priority: int = PortPriority.BUILTIN, label: str) -> None:
        self._registry.register(port_name, factory, priority=priority, label=label, plugin=self._manifest.name)

    def publish(self, key: str, value: Any) -> None:
        self._objects[key] = value

    def shared(self, key: str, default: Any = None) -> Any:
        return self._objects.get(key, default)

    def expose_skill_gateway(self, gateway: Any) -> None:
        self._skill_gateway = gateway

    @property
    def skill_gateway(self) -> Any:
        return self._skill_gateway

    def attach_stdio_registry(self, registry: Any) -> None:
        """挂载 stdio 传输注册表：同时写入装配产出与已暴露的 SkillGateway。"""
        self._stdio_registry = registry
        if self._skill_gateway is not None:
            self._skill_gateway.stdio_registry = registry

    @property
    def stdio_registry(self) -> Any:
        return self._stdio_registry


# 历史别名：装配上下文即插件 SDK 的视图（插件 contribute 形参类型）
AssemblyContext = PluginSdk


class BuiltinPlugin(Protocol):
    manifest: PluginManifest

    def contribute(self, sdk: PluginSdk) -> None: ...


def validate_manifest(manifest: PluginManifest, known_ports: set[str]) -> None:
    unknown_provides = [name for name in manifest.provides if name not in known_ports]
    if unknown_provides:
        raise ValueError(f"plugin {manifest.name} provides unknown ports: {unknown_provides}")
    unknown_requires = [name for name in manifest.requires if name not in known_ports]
    if unknown_requires:
        raise ValueError(f"plugin {manifest.name} requires unknown ports: {unknown_requires}")


# ---- Module 装配层（契约 §4：Profile → Module → Plugin）----


@dataclass(frozen=True)
class ModuleSpec:
    """Module：承载一个相对完整的业务/基础能力域，声明构成插件与所需 Port。

    plugins 顺序即展开后 contribute 顺序；requires_ports 在装配 resolve 后校验
    （缺失抛 AssemblyError 拒绝启动），实现"Module 声明需要哪些 Port"。
    """

    name: str
    description: str
    plugins: tuple[str, ...] = ()
    requires_ports: tuple[str, ...] = ()


def validate_module(spec: ModuleSpec, known_plugins: dict[str, Any]) -> None:
    unknown = [name for name in spec.plugins if name not in known_plugins]
    if unknown:
        raise ValueError(f"module {spec.name} references unknown plugins: {unknown}")
