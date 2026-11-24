"""能力声明与四级阶梯（Phase 2 §四：manifest-first 校验 + capability ladder）。

两条不变量：
1. **manifest-first**：插件对外提供能力（注册 Port / 发布共享服务）必须先在
   manifest 声明；`PluginSdk.register_port` / `publish` 在执行点强制校验。
2. **capability ladder**：新增能力按引入代价分四级，越靠后代价越高；
   `core_change`（新增内核 Port / 改核心契约）必须带审批引用，装配期拒绝无审批的核心变更。

本模块属于 `app.plugin_sdk` 公开面，插件与内核都可直接依赖，不引入新内核 Port。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.plugin_sdk.errors import AssemblyError


class CapabilityLevel(str, Enum):
    """能力阶梯（由轻到重）。"""

    EXTEND_EXISTING = "extend_existing"
    EXISTING_CONTRACT = "existing_contract"
    NARROW_EXTENSION = "narrow_extension"
    CORE_CHANGE = "core_change"


CAPABILITY_LEVELS: tuple[str, ...] = tuple(level.value for level in CapabilityLevel)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")


@dataclass(frozen=True)
class CapabilityDeclaration:
    """一条能力声明：名称 + 阶梯层级 + 用途 + （核心变更必需的）审批引用。"""

    name: str
    level: CapabilityLevel | str
    description: str = ""
    approval_ref: str | None = None

    def __post_init__(self) -> None:
        level = self.level.value if isinstance(self.level, CapabilityLevel) else self.level
        if level not in CAPABILITY_LEVELS:
            raise AssemblyError(f"capability {self.name!r} has unknown ladder level {level!r}")
        if not isinstance(self.name, str) or not _NAME_RE.match(self.name):
            raise AssemblyError(f"invalid capability name {self.name!r}")
        if level == CapabilityLevel.CORE_CHANGE.value and not (self.approval_ref or "").strip():
            raise AssemblyError(
                f"capability {self.name!r} is a core_change and requires an approval_ref"
            )

    @property
    def level_value(self) -> str:
        return self.level.value if isinstance(self.level, CapabilityLevel) else self.level


def validate_capability_declarations(declarations: tuple[CapabilityDeclaration, ...] | list[CapabilityDeclaration]) -> None:
    """校验单插件能力声明：层级合法 + 名称唯一（插件内不允许重复声明同一能力）。"""
    seen: set[str] = set()
    for declaration in declarations or ():
        if not isinstance(declaration, CapabilityDeclaration):
            raise AssemblyError(f"capability declaration must be CapabilityDeclaration, got {type(declaration).__name__}")
        if declaration.name in seen:
            raise AssemblyError(f"duplicate capability declaration: {declaration.name}")
        seen.add(declaration.name)


def summarize_governance(manifests: tuple[Any, ...] | list[Any]) -> dict[str, Any]:
    """汇总装配内全部能力声明，生成 capabilities 上报用的治理摘要。

    - `manifest_first` 恒为 True（门禁在 PluginSdk 执行点强制）；
    - `levels` 四级计数，未声明能力的插件不计入；
    - `declarations` 为 name → {plugin, level, description, approval_ref}；
    - `shared_capabilities` 为被多个插件同时声明的能力名（需关注但非错误：
      例如多个插件共同扩展同一能力；core_change 在构造时已强制审批）。
    """
    declarations: dict[str, dict[str, Any]] = {}
    owners: dict[str, set[str]] = {}
    levels = {level: 0 for level in CAPABILITY_LEVELS}
    for manifest in manifests:
        for declaration in getattr(manifest, "capabilities", ()) or ():
            levels[declaration.level_value] = levels.get(declaration.level_value, 0) + 1
            declarations[declaration.name] = {
                "plugin": manifest.name,
                "level": declaration.level_value,
                "description": declaration.description,
                "approval_ref": declaration.approval_ref,
            }
            owners.setdefault(declaration.name, set()).add(manifest.name)
    return {
        "manifest_first": True,
        "levels": levels,
        "declarations": declarations,
        "shared_capabilities": sorted(name for name, plugins in owners.items() if len(plugins) > 1),
    }
