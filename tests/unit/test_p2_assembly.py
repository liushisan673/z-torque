"""P2 架构项测试：stage 动态化（①）/ 插件 SDK（②）/ Module 装配层（③）。"""

from types import SimpleNamespace

import pytest

from app.assembly import assemble, expand_profile
from app.builtin_plugins.manifest import ModuleSpec, PluginSdk
from app.builtin_plugins.modules import MODULE_REGISTRY, PROFILE_COMMUNITY
from app.config import get_settings
from app.db.session import engine
from app.kernel.capabilities import derive_stage
from app.kernel.registry.port_registry import AssemblyError, PortRegistry, PortState
from app.kernel.ports.specs import PORT_SPECS
from app.builtin_plugins.plugins import BUILTIN_PLUGINS


# ---- ① stage 动态化 ----


def test_derive_stage_full_assembly_is_11():
    result = assemble(get_settings(), engine)
    assert derive_stage(result.registry) == "11"


def test_derive_stage_degrades_when_gate_port_missing():
    specs = tuple(s for s in PORT_SPECS if s.name not in {"connector", "memory.short", "object.store", "secret.provider", "model.selector", "lock.provider"})
    registry = PortRegistry(specs)
    for spec in specs:
        registry.register(spec.name, lambda: object(), label="fake")
    registry.resolve()
    # connector 等缺失 → 只满足 stage 3 门槛（model/skill/queue 均在列）
    assert derive_stage(registry) == "3"


def test_derive_stage_none_registry_returns_baseline():
    assert derive_stage(None) == "1"


# ---- ② 插件 SDK ----


def test_sdk_register_port_attributes_plugin():
    from app.builtin_plugins.notify_channel import LoggingNotifyChannel, NotifyChannelPlugin

    registry = PortRegistry((next(s for s in PORT_SPECS if s.name == "notify.channel"),))
    plugin = NotifyChannelPlugin()
    plugin.contribute(PluginSdk.of(plugin, registry=registry))
    registry.resolve()
    assert registry.resolution("notify.channel").plugin == "notify_channel"
    assert isinstance(registry.get("notify.channel"), LoggingNotifyChannel)


def test_sdk_engine_gated_by_db_query_permission():
    from app.builtin_plugins.plugins import QueueDriverPlugin

    sdk = PluginSdk.of(QueueDriverPlugin(), engine=object())  # queue_driver 未声明 db.query
    with pytest.raises(PermissionError, match="db.query"):
        _ = sdk.engine


def test_sdk_engine_allowed_with_permission():
    from app.builtin_plugins.plugins import SkillExecutorPlugin

    marker = object()
    sdk = PluginSdk.of(SkillExecutorPlugin(), engine=marker)  # skill_executors 声明了 db.query
    assert sdk.engine is marker


def test_sdk_publish_shared_roundtrip():
    sdk = PluginSdk(objects={})
    sdk.publish("k", {"a": 1})
    assert sdk.shared("k") == {"a": 1}
    assert sdk.shared("missing", default="d") == "d"


# ---- ③ Module 装配层 ----


def test_profile_community_expands_to_all_19_plugins():
    expanded = expand_profile(PROFILE_COMMUNITY)
    assert len(expanded) == 19
    assert set(expanded) == {p.manifest.name for p in BUILTIN_PLUGINS}
    # 依赖顺序约束：secret 先于 model_provider；authz/approval/sandbox/interceptor 先于 skill_executors；mcp 最后于 skill_executors
    order = {name: i for i, name in enumerate(expanded)}
    assert order["secret_provider"] < order["model_provider"]
    assert all(order[n] < order["skill_executors"] for n in ("authz_provider", "approval_provider", "sandbox_driver", "interceptor"))
    assert order["skill_executors"] < order["mcp_connector"]


def test_profile_rejects_unknown_entry():
    with pytest.raises(AssemblyError, match="unknown module or plugin"):
        expand_profile(("no_such_thing",))


def test_module_requires_ports_enforced():
    """模块级 requires_ports：vector.store 无实现（UNAVAILABLE）→ 合成模块 fail fast。"""
    import app.builtin_plugins.modules as modules_mod

    monkey_spec = ModuleSpec(name="synthetic", description="t", plugins=(), requires_ports=("vector.store",))
    modules_mod.MODULE_REGISTRY[monkey_spec.name] = monkey_spec
    try:
        with pytest.raises(AssemblyError, match="module synthetic requires port vector.store"):
            assemble(get_settings(), engine, profile=(*PROFILE_COMMUNITY, "synthetic"))
    finally:
        modules_mod.MODULE_REGISTRY.pop(monkey_spec.name, None)
    assert "synthetic" not in MODULE_REGISTRY


def test_assembly_result_smoke_after_module_refactor():
    result = assemble(get_settings(), engine)
    resolved = [item for item in result.registry.status() if item["state"] == PortState.RESOLVED]
    assert len(result.plugins) == 19
    assert len(resolved) == 19


def test_sdk_accepts_legacy_duck_context_shape():
    """SDK 形状回归：settings/engine/registry/objects 属性面与旧 AssemblyContext 等价。"""
    sdk = PluginSdk(settings=SimpleNamespace(x=1), engine="e", registry="r", objects={})
    assert sdk.settings.x == 1 and sdk.engine == "e" and sdk.registry == "r"
