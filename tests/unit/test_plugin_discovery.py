"""插件发现（阶段 2）：内置元组兜底 + entry_points 追加。

红线（社区默认路径不拆）：无 entry_points 时发现结果必须与 BUILTIN_PLUGINS 完全一致，
否则本地/CI 会因为环境差异装出不同的插件集合。
"""

from __future__ import annotations

import pytest

from app.assembly import assemble, expand_profile
from app.builtin_plugins.discovery import (
    ENTRY_POINT_GROUP,
    DiscoveredPlugin,
    clear_discovery_cache,
    discover_plugins,
)
from app.builtin_plugins.manifest import PluginManifest, PluginSdk
from app.builtin_plugins.modules import PROFILE_COMMUNITY
from app.builtin_plugins.notify_channel import LoggingNotifyChannel
from app.builtin_plugins.plugins import BUILTIN_PLUGINS
from app.config import get_settings
from app.db.session import engine
from app.kernel.registry.port_registry import AssemblyError


class _FakeEntryPoint:
    """importlib.metadata.EntryPoint 的最小替身。"""

    def __init__(self, name: str, target: object, *, value: str = "pkg.plugin:Plugin") -> None:
        self.name = name
        self.value = value
        self._target = target

    def load(self) -> object:
        if isinstance(self._target, BaseException):
            raise self._target
        return self._target


class ThirdPartyNotifyPlugin:
    """模拟企业版/三方包：只实现一个可选 Port，覆盖内置通知渠道。"""

    manifest = PluginManifest(
        name="third_party_notify",
        description="entry point 发现的三方通知渠道（覆盖内置 LoggingNotifyChannel）",
        provides=(),
    )

    def contribute(self, sdk: PluginSdk) -> None:
        sdk.register_port("notify.channel", LoggingNotifyChannel, label="third_party_notify")


def test_discovery_without_entry_points_matches_builtin_tuple():
    clear_discovery_cache()
    discovered = discover_plugins(entry_points=[])
    assert [item.name for item in discovered] == [cls.manifest.name for cls in BUILTIN_PLUGINS]
    assert all(item.builtin for item in discovered)
    assert all(item.origin == "builtin" for item in discovered)


def test_entry_point_plugin_is_appended_after_builtins():
    discovered = discover_plugins(entry_points=[_FakeEntryPoint("third_party_notify", ThirdPartyNotifyPlugin)])
    names = [item.name for item in discovered]
    assert names[0] == BUILTIN_PLUGINS[0].manifest.name, "内置顺序即装配顺序，不得被 entry point 打乱"
    assert names[-1] == "third_party_notify"
    appended = discovered[-1]
    assert appended.builtin is False
    assert appended.origin == "entry_point:third_party_notify"


def test_entry_point_duplicate_name_is_ignored():
    duplicate = _FakeEntryPoint("dupe", ThirdPartyNotifyPlugin)
    discovered = discover_plugins(
        entry_points=[_FakeEntryPoint("queue_driver", ThirdPartyNotifyPlugin), duplicate]
    )
    # 两次提供同名插件：第一次已登记，第二次被忽略；内置的 queue_driver 不受影响
    assert [item.name for item in discovered].count("third_party_notify") == 1
    assert sum(1 for item in discovered if item.name == "queue_driver") == 1


def test_entry_point_with_builtin_name_does_not_override_builtin():
    class ShadowQueueDriver:
        manifest = PluginManifest(name="queue_driver", description="恶意同名", provides=("queue.driver",))

        def contribute(self, sdk: PluginSdk) -> None:  # pragma: no cover - 不应被调用
            raise AssertionError("builtin definition must win over entry point")

    discovered = discover_plugins(entry_points=[_FakeEntryPoint("shadow", ShadowQueueDriver)])
    matches = [item for item in discovered if item.name == "queue_driver"]
    assert len(matches) == 1
    assert matches[0].builtin is True


def test_broken_entry_point_is_skipped_by_default():
    discovered = discover_plugins(
        entry_points=[
            _FakeEntryPoint("broken", ImportError("missing dependency")),
            _FakeEntryPoint("third_party_notify", ThirdPartyNotifyPlugin),
        ]
    )
    # 一个坏的三方包不该让服务起不来（社区默认宽松），但不能影响其他插件的发现
    assert [item.name for item in discovered][-1] == "third_party_notify"


def test_broken_entry_point_raises_in_strict_mode():
    with pytest.raises(AssemblyError, match="cannot load plugin entry point"):
        discover_plugins(entry_points=[_FakeEntryPoint("broken", ImportError("boom"))], strict=True)


def test_entry_point_pointing_at_non_plugin_is_rejected():
    def not_a_plugin() -> None:  # pragma: no cover - 只用于形状校验
        return None

    discovered = discover_plugins(entry_points=[_FakeEntryPoint("func", not_a_plugin)])
    assert "func" not in [item.name for item in discovered]
    with pytest.raises(AssemblyError, match="did not resolve to a plugin class"):
        discover_plugins(entry_points=[_FakeEntryPoint("func", not_a_plugin)], strict=True)


def test_expand_profile_accepts_discovered_plugin_name():
    discovered = discover_plugins(entry_points=[_FakeEntryPoint("third_party_notify", ThirdPartyNotifyPlugin)])
    expanded = expand_profile((*PROFILE_COMMUNITY, "third_party_notify"), discovered)
    assert expanded[-1] == "third_party_notify"


def test_undiscovered_plugin_name_in_profile_still_fails():
    discovered = discover_plugins(entry_points=[])
    with pytest.raises(AssemblyError, match="unknown module or plugin"):
        expand_profile((*PROFILE_COMMUNITY, "third_party_notify"), discovered)


def test_assembled_third_party_plugin_registers_with_plugin_priority(monkeypatch):
    """装配级验收：entry point 插件由 Profile 选中后真的 contribute，且优先级是 PLUGIN。"""
    discovered = discover_plugins(entry_points=[_FakeEntryPoint("third_party_notify", ThirdPartyNotifyPlugin)])
    monkeypatch.setattr("app.assembly.discover_plugins", lambda **_: discovered)

    result = assemble(get_settings(), engine, (*PROFILE_COMMUNITY, "third_party_notify"))

    resolution = result.registry.resolution("notify.channel")
    # 内置 LoggingNotifyChannel 与三方实现同 Port：插件优先级更高（契约 §3 显式配置 > 插件 > 内置）
    assert resolution.plugin == "third_party_notify"
    assert "third_party_notify" in {manifest.name for manifest in result.plugins}
    assert DiscoveredPlugin(
        cls=ThirdPartyNotifyPlugin, builtin=False, origin="entry_point:third_party_notify"
    ).name == "third_party_notify"


def test_entry_point_group_is_stable():
    # 企业版 pyproject 依赖这个字符串，改名属于破坏性变更
    assert ENTRY_POINT_GROUP == "ztorque.plugins"
