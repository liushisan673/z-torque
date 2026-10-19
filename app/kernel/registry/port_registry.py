"""Port 注册表：注册、按优先级解析、启动校验与能力查询。

解析规则（阶段00 §3.5）：显式配置 > 插件优先级 > 内置默认 > UNAVAILABLE。
- 同优先级存在多个候选且未显式指定 → AssemblyError（启动失败）。
- 必需 Port（pending=False）无候选 → AssemblyError（fail fast）。
- 必需 Port（pending=True）无候选 → NOT_PORTED，仅告警（阶段01 §3.11：区分"尚未迁移"与"不可用"）。
- 可选 Port（pending=False）无候选 → UNAVAILABLE，调用时抛 AI_CAPABILITY_UNAVAILABLE。
- 可选 Port（pending=True）无候选 → NOT_PORTED。

约束：本包属于内核，禁止导入 app.builtin_plugins（阶段00 §3.8 约束 1）。
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import ApiError
from app.kernel.ports.specs import PORT_SPEC_BY_NAME, GROUPS, PortSpec

logger = logging.getLogger("z_torque.port_registry")


class AssemblyError(Exception):
    """装配/解析失败：必需 Port 缺失或同优先级歧义，启动必须终止。"""


class PortState:
    RESOLVED = "RESOLVED"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_PORTED = "NOT_PORTED"


class PortPriority:
    """数值越大优先级越高：显式配置 > 插件 > 内置默认。"""

    BUILTIN = 1
    PLUGIN = 2
    EXPLICIT = 3


@dataclass(frozen=True)
class PortRegistration:
    port_name: str
    factory: Callable[[], Any]
    priority: int
    label: str
    plugin: str | None = None


@dataclass
class PortResolution:
    spec: PortSpec
    state: str
    label: str | None = None
    plugin: str | None = None
    priority: int | None = None
    candidates: list[str] = field(default_factory=list)
    instance: Any = None
    error: str | None = None


class UnavailableCapability:
    """可选 Port 缺失时的占位对象：任何属性访问都抛 AI_CAPABILITY_UNAVAILABLE。

    调用方拿到它即视为"能力显式不可用"，不会伪装成功（阶段00 §3.8 约束 9）。
    审计链接入由调用侧网关在捕获该错误时落库。
    """

    def __init__(self, port_name: str) -> None:
        object.__setattr__(self, "_port_name", port_name)

    def __getattr__(self, item: str) -> Any:
        port_name: str = object.__getattribute__(self, "_port_name")
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", f"port {port_name} is not available", 503)


class PortRegistry:
    def __init__(self, specs: tuple[PortSpec, ...] | None = None) -> None:
        self._specs: dict[str, PortSpec] = dict(PORT_SPEC_BY_NAME)
        if specs is not None:
            self._specs = {spec.name: spec for spec in specs}
        self._registrations: dict[str, list[PortRegistration]] = {name: [] for name in self._specs}
        self._resolutions: dict[str, PortResolution] = {}

    # ---- 注册 ----

    def register(self, port_name: str, factory: Callable[[], Any], *, priority: int = PortPriority.BUILTIN, label: str, plugin: str | None = None) -> None:
        if port_name not in self._specs:
            raise AssemblyError(f"unknown port: {port_name}（只允许实现内核已定义的 Port，新增走契约变更流程）")
        if priority not in (PortPriority.BUILTIN, PortPriority.PLUGIN, PortPriority.EXPLICIT):
            raise AssemblyError(f"invalid priority {priority} for port {port_name}")
        self._registrations[port_name].append(PortRegistration(port_name, factory, priority, label, plugin))

    # ---- 解析与校验 ----

    def resolve(self) -> None:
        """解析全部 Port 并执行启动校验；失败抛 AssemblyError。幂等：重复调用先清空上次结果。"""
        self._resolutions = {}
        for name, spec in self._specs.items():
            self._resolutions[name] = self._resolve_one(name, spec)
        failures = [f"{name}: {resolution.error}" for name, resolution in self._resolutions.items() if resolution.error]
        if failures:
            raise AssemblyError("; ".join(failures))

    def _resolve_one(self, name: str, spec: PortSpec) -> PortResolution:
        candidates = sorted(self._registrations[name], key=lambda item: item.priority, reverse=True)
        resolution = PortResolution(spec=spec, state=PortState.NOT_PORTED, candidates=[f"{item.label}@p{item.priority}" for item in candidates])
        if not candidates:
            if spec.required and not spec.pending:
                resolution.error = f"required port {name} has no implementation"
            elif spec.pending:
                logger.warning("port %s is declared but not ported yet (pending migration)", name)
            else:
                resolution.state = PortState.UNAVAILABLE
            return resolution
        top = candidates[0]
        same_level = [item for item in candidates if item.priority == top.priority]
        if len(same_level) > 1:
            resolution.error = f"ambiguous port {name}: {len(same_level)} candidates at priority {top.priority}; set an explicit configuration"
            return resolution
        resolution.state = PortState.RESOLVED
        resolution.label = top.label
        resolution.plugin = top.plugin
        resolution.priority = top.priority
        try:
            resolution.instance = top.factory()
        except Exception as exc:
            resolution.error = f"port {name} factory failed: {exc}"
        return resolution

    # ---- 访问 ----

    def try_get(self, port_name: str) -> Any | None:
        resolution = self._resolutions.get(port_name)
        if resolution is None or resolution.state != PortState.RESOLVED:
            return None
        return resolution.instance

    def get(self, port_name: str) -> Any:
        resolution = self._resolutions.get(port_name)
        if resolution is None:
            raise AssemblyError(f"unknown port: {port_name}")
        if resolution.state == PortState.RESOLVED:
            return resolution.instance
        return UnavailableCapability(port_name)

    def resolution(self, port_name: str) -> PortResolution | None:
        return self._resolutions.get(port_name)

    # ---- 能力查询（/capabilities 数据源）----

    def status(self) -> list[dict[str, Any]]:
        report: list[dict[str, Any]] = []
        for name in sorted(self._specs):
            resolution = self._resolutions.get(name)
            spec = self._specs[name]
            item: dict[str, Any] = {
                "name": name,
                "group": spec.group,
                "required": spec.required,
                "description": spec.description,
                "community_default": spec.community_default,
                "state": resolution.state if resolution else PortState.NOT_PORTED,
            }
            if resolution is not None:
                if resolution.label:
                    item["implementation"] = resolution.label
                if resolution.plugin:
                    item["plugin"] = resolution.plugin
                item["available"] = resolution.state == PortState.RESOLVED
            report.append(item)
        return report

    def group_overview(self) -> dict[str, dict[str, int]]:
        overview: dict[str, dict[str, int]] = {group: {"total": 0, "resolved": 0} for group in GROUPS}
        for name, spec in self._specs.items():
            overview[spec.group]["total"] += 1
            resolution = self._resolutions.get(name)
            if resolution is not None and resolution.state == PortState.RESOLVED:
                overview[spec.group]["resolved"] += 1
        return overview
