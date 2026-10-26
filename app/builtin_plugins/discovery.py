"""插件发现（标准插件可移植性 · 阶段 2）。

两条路径，顺序固定：

1. **内置清单** `BUILTIN_PLUGINS`（`app/builtin_plugins/plugins.py` 的元组）：
   社区版默认路径，顺序即装配顺序，不依赖运行期元数据 —— 本地 / CI / 无
   entry_points 的环境行为完全一致（不拆社区默认路径）。
2. **entry_points**（group `ztorque.plugins`）：企业版或三方包**追加**插件。
   重名以内置为准（告警并忽略 entry point）；加载失败默认跳过并告警
   —— 一个坏的三方包不该让服务起不来；`strict=True` 时改为抛 AssemblyError
   （企业版可要求"声明的插件必须装上"）。

发现的插件**不会自动装配**：仍需出现在 Profile（或 `assemble(extra_plugins=...)`）里，
与内置插件同规则 —— 避免"装个包就悄悄改了运行时行为"。

entry_points 插件的 Port 注册走 `PortPriority.PLUGIN`（builtin=False），
因此不会静默覆盖内置实现，仍服从「显式配置 > 插件 > 内置默认」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Iterable

from app.builtin_plugins.plugins import BUILTIN_PLUGINS

logger = logging.getLogger("z_torque.plugins.discovery")

# entry point group：企业版包在 pyproject 里写
#   [project.entry-points."ztorque.plugins"]
#   my_plugin = "my_pkg.plugin:MyPlugin"
ENTRY_POINT_GROUP = "ztorque.plugins"


@dataclass(frozen=True)
class DiscoveredPlugin:
    """一条发现结果：插件类 + 来源（决定 Port 优先级与错误语义）。"""

    cls: Any
    builtin: bool
    origin: str

    @property
    def name(self) -> str:
        return self.cls.manifest.name


_cache: tuple[DiscoveredPlugin, ...] | None = None


def _looks_like_plugin(candidate: Any) -> bool:
    """插件类的最小形状：类型 + manifest + 可调用 contribute。

    这里刻意不复用 PluginManifest 的结构校验（那在 validate_manifest 里做），
    只挡住"entry point 指到了函数/模块/常量"这类明显误配。
    """
    return (
        isinstance(candidate, type)
        and hasattr(candidate, "manifest")
        and callable(getattr(candidate, "contribute", None))
    )


def _entry_points(group: str) -> Iterable[Any]:
    try:
        return metadata.entry_points(group=group)
    except TypeError:  # pragma: no cover - 兼容旧版 importlib.metadata 签名
        return metadata.entry_points().get(group, ())


def _load_entry_point_plugins(
    entry_points: Iterable[Any] | None, *, strict: bool
) -> list[DiscoveredPlugin]:
    source = entry_points if entry_points is not None else _entry_points(ENTRY_POINT_GROUP)
    found: list[DiscoveredPlugin] = []
    for ep in source:
        name = getattr(ep, "name", "<unknown>")
        value = getattr(ep, "value", "<unknown>")
        try:
            candidate = ep.load()
        except Exception as exc:
            message = f"cannot load plugin entry point {name!r} ({value}): {exc}"
            if strict:
                from app.plugin_sdk import AssemblyError

                raise AssemblyError(message) from exc
            logger.warning("%s -- skipped (strict mode would fail startup)", message, exc_info=True)
            continue
        if not _looks_like_plugin(candidate):
            message = (
                f"plugin entry point {name!r} ({value}) did not resolve to a plugin class "
                f"(needs class attributes: manifest, contribute)"
            )
            if strict:
                from app.plugin_sdk import AssemblyError

                raise AssemblyError(message)
            logger.warning("%s -- skipped", message)
            continue
        found.append(DiscoveredPlugin(cls=candidate, builtin=False, origin=f"entry_point:{name}"))
    return found


def discover_plugins(
    *,
    entry_points: Iterable[Any] | None = None,
    strict: bool = False,
) -> tuple[DiscoveredPlugin, ...]:
    """返回全部可发现的插件（内置在前）。

    参数仅用于测试与"显式清单"两种场景：
    - `entry_points` 显式给出时替换运行期 entry point 扫描（不读缓存）；
    - `strict=True` 时 entry point 加载失败直接抛 AssemblyError（不读缓存）。
    """
    global _cache
    if entry_points is None and not strict and _cache is not None:
        return _cache

    discovered: list[DiscoveredPlugin] = [
        DiscoveredPlugin(cls=plugin_cls, builtin=True, origin="builtin")
        for plugin_cls in BUILTIN_PLUGINS
    ]
    seen = {item.name for item in discovered}
    for item in _load_entry_point_plugins(entry_points, strict=strict):
        if item.name in seen:
            logger.warning(
                "plugin %r from %s duplicates an already discovered plugin name; "
                "the earlier (builtin) definition wins",
                item.name,
                item.origin,
            )
            continue
        seen.add(item.name)
        discovered.append(item)

    result = tuple(discovered)
    if entry_points is None and not strict:
        _cache = result
    return result


def clear_discovery_cache() -> None:
    """清空发现缓存（测试 / 运行期安装插件后重新发现）。"""
    global _cache
    _cache = None
