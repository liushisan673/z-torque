"""Profile 选配（`基础档+条目`）：社区版按需装上企业插件。

红线：两个内置档的展开结果必须逐字节不变 —— 选配语法只能是**追加**，
不得改变 community / enterprise 的默认装配集合（否则本地/CI 又会被环境差异带偏）。
"""

from __future__ import annotations

import pytest

from app.assembly import expand_profile
from app.builtin_plugins.modules import (
    PROFILE_COMMUNITY,
    PROFILE_ENTERPRISE,
    resolve_profile,
)
from app.config import get_settings
from app.kernel.registry.port_registry import AssemblyError


def test_builtin_profiles_expand_unchanged():
    community = expand_profile(resolve_profile("community"))
    enterprise = expand_profile(resolve_profile("enterprise"))
    # 社区默认路径不得被选配语法改变：仍是不含两个企业插件的集合
    assert "audit_export_archive" not in community and "a2a_gateway" not in community
    assert set(enterprise) - set(community) == {"audit_export_archive", "a2a_gateway"}
    assert enterprise[: len(community)] == community, "企业档是社区集合的追加，前缀必须一致"


def test_default_profile_without_name_is_community():
    assert resolve_profile(None) == PROFILE_COMMUNITY
    assert resolve_profile("  COMMUNITY ") == PROFILE_COMMUNITY


def test_community_plus_selects_enterprise_plugins_on_top_of_community():
    profile = resolve_profile("community+audit_export_archive,a2a")
    assert profile[: len(PROFILE_COMMUNITY)] == PROFILE_COMMUNITY
    expanded = expand_profile(profile)
    # 追加顺序即 contribute 顺序：审计导出归档先于 a2a（a2a 依赖 skill.executor）
    assert expanded.index("audit_export_archive") < expanded.index("a2a_gateway")
    assert {"sandbox_driver", "audit_export_archive", "a2a_gateway"} <= set(expanded)
    assert len(expanded) == len(set(expanded)), "选配不得让同一插件被装配两次"


def test_reselecting_plugin_already_in_base_is_rejected():
    # execution 模块（含 sandbox_driver）已在社区档里：重复选配要拒绝，不能静默通过
    with pytest.raises(AssemblyError, match="already provided"):
        resolve_profile("community+execution")
    with pytest.raises(AssemblyError, match="already provided"):
        resolve_profile("community+sandbox_driver")
    # 模块名与插件名混用也能识破重复（a2a 展开就是 a2a_gateway）
    with pytest.raises(AssemblyError, match="already provided"):
        resolve_profile("enterprise+a2a")


def test_empty_or_unknown_selection_is_rejected_at_assembly():
    with pytest.raises(AssemblyError, match="empty selection list"):
        resolve_profile("community+")
    with pytest.raises(AssemblyError, match="unknown assembly profile"):
        resolve_profile("unknown+audit_export_archive")
    # 条目写错：形状合法但不存在 → expand_profile 用运行时发现判定并拒绝启动
    with pytest.raises(AssemblyError, match="unknown module or plugin"):
        expand_profile(resolve_profile("community+no_such_plugin"))


def test_settings_accept_selection_syntax_but_reject_malformed():
    get_settings().model_copy(update={"assembly_profile": "community+audit_export_archive,a2a"}).validate_runtime()
    for bad in ("community+", "community+,", "unknown+audit_export_archive", "community+a2a,audit-export"):
        settings = get_settings().model_copy(update={"assembly_profile": bad})
        with pytest.raises(ValueError, match="ASSEMBLY_PROFILE"):
            settings.validate_runtime()


def test_enterprise_profile_is_still_selectable_as_base():
    # 企业档已含两个企业插件 → 不能再追加它们；不带追加时行为完全不变
    assert resolve_profile("enterprise") == PROFILE_ENTERPRISE
    assert resolve_profile("enterprise+nonexistent_plugin")[-1] == "nonexistent_plugin"
