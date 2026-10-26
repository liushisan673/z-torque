"""manifest 契约（阶段 2/3）：api_version 兼容性 + 插件名唯一性。

可移植性的前提是"装上就不会语义错配"：
- 内核不认识 api_version → 拒绝启动（而不是半懂地跑）；
- 同名插件 → 拒绝启动（而不是让后写的静默顶掉先写的）。
"""

from __future__ import annotations

import pytest

from app.assembly import assemble
from app.builtin_plugins.manifest import PluginManifest
from app.builtin_plugins.modules import PROFILE_COMMUNITY
from app.config import get_settings
from app.db.session import engine
from app.kernel.registry.port_registry import PORT_SPEC_BY_NAME, AssemblyError
from app.plugin_sdk import PLUGIN_API_VERSION, SUPPORTED_API_VERSIONS, validate_manifest


class _NotifyOverride:
    """一个只覆盖可选 Port 的合法三方插件。"""

    def __init__(self, name: str) -> None:
        self.manifest = PluginManifest(name=name, description="三方覆盖", provides=())

    def contribute(self, sdk) -> None:  # pragma: no cover - 不应走到这里
        raise AssertionError("should be rejected before contribute")


def test_default_api_version_is_supported():
    assert PLUGIN_API_VERSION in SUPPORTED_API_VERSIONS
    manifest = PluginManifest(name="any_plugin", description="", provides=())
    validate_manifest(manifest, set(PORT_SPEC_BY_NAME))


def test_unknown_api_version_is_rejected_with_supported_list():
    manifest = PluginManifest(
        name="future_plugin",
        description="按未来的契约写的",
        provides=(),
        api_version="zsoftym.ai/plugin.v99",
    )
    with pytest.raises(ValueError, match="unsupported api_version") as excinfo:
        validate_manifest(manifest, set(PORT_SPEC_BY_NAME))
    # 错误信息要能直接告诉对方"内核认哪些版本"
    assert PLUGIN_API_VERSION in str(excinfo.value)


def test_duplicate_names_within_extra_plugins_are_rejected():
    with pytest.raises(AssemblyError, match="duplicate plugin names"):
        assemble(
            get_settings(),
            engine,
            PROFILE_COMMUNITY,
            extra_plugins=(_NotifyOverride("same_name"), _NotifyOverride("same_name")),
        )


def test_extra_plugin_colliding_with_profile_plugin_is_rejected():
    with pytest.raises(AssemblyError, match="both by the profile and extra_plugins"):
        assemble(
            get_settings(),
            engine,
            PROFILE_COMMUNITY,
            extra_plugins=(_NotifyOverride("queue_driver"),),
        )
