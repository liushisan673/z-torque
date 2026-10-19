"""内置插件契约：manifest 与插件接口（阶段00 §3.6 / 契约 §5 最小纪律）。

- manifest 唯一入口：声明 apiVersion / provides / requires / permissions。
- 插件只实现内核已定义的 Port（registry.register 对未知 Port 直接拒绝）。
- 内核（app/kernel、app/api、app/services）禁止导入本包；仅装配根
  app/assembly.py 允许加载插件。
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

PLUGIN_API_VERSION = "zsoftym.ai/plugin.v1"


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


class AssemblyContext(Protocol):
    """装配上下文：插件经它读取配置、注册 Port、共享装配期对象。"""

    settings: Any
    engine: Any
    registry: Any
    skill_gateway: Any  # 由 skill.executor 插件创建后写入，供传输类插件挂载
    stdio_registry: Any
    objects: dict[str, Any]


class BuiltinPlugin(Protocol):
    manifest: PluginManifest

    def contribute(self, context: Any) -> None: ...


def validate_manifest(manifest: PluginManifest, known_ports: set[str]) -> None:
    unknown_provides = [name for name in manifest.provides if name not in known_ports]
    if unknown_provides:
        raise ValueError(f"plugin {manifest.name} provides unknown ports: {unknown_provides}")
    unknown_requires = [name for name in manifest.requires if name not in known_ports]
    if unknown_requires:
        raise ValueError(f"plugin {manifest.name} requires unknown ports: {unknown_requires}")
