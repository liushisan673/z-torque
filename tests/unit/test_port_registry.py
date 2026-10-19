"""PortRegistry 解析规则与启动校验测试（阶段00 §3.5 / 阶段01 §3.11）。"""

import pytest

from app.core.errors import ApiError
from app.kernel.ports.specs import PORT_SPECS
from app.kernel.registry.port_registry import (
    AssemblyError,
    PortPriority,
    PortRegistry,
    PortState,
    UnavailableCapability,
)


def test_specs_lock_21_ports_in_four_groups():
    groups = {spec.group for spec in PORT_SPECS}
    assert len(PORT_SPECS) == 21
    assert groups == {"entry", "execution", "information", "cross_cutting"}


def test_unknown_port_registration_rejected():
    registry = PortRegistry()
    with pytest.raises(AssemblyError, match="unknown port"):
        registry.register("not.a.port", lambda: object(), label="x")


def test_required_port_missing_fails_startup():
    spec = PORT_SPECS[3]  # 任取一个 required
    spec = next(s for s in PORT_SPECS if s.required and not s.pending)
    registry = PortRegistry((spec,))
    with pytest.raises(AssemblyError, match="required port"):
        registry.resolve()


def test_pending_port_missing_is_not_ported_not_failure():
    spec = next(s for s in PORT_SPECS if s.required and s.pending)
    registry = PortRegistry((spec,))
    registry.resolve()  # 不抛异常
    resolution = registry.resolution(spec.name)
    assert resolution.state == PortState.NOT_PORTED
    assert registry.try_get(spec.name) is None


def test_plugin_priority_overrides_builtin():
    spec = next(s for s in PORT_SPECS if s.required and not s.pending)
    builtin_marker = object()
    plugin_marker = object()
    registry = PortRegistry((spec,))
    registry.register(spec.name, lambda: builtin_marker, priority=PortPriority.BUILTIN, label="builtin")
    registry.register(spec.name, lambda: plugin_marker, priority=PortPriority.PLUGIN, label="plugin")
    registry.resolve()
    assert registry.get(spec.name) is plugin_marker


def test_same_priority_ambiguity_fails_startup():
    spec = next(s for s in PORT_SPECS if s.required and not s.pending)
    registry = PortRegistry((spec,))
    registry.register(spec.name, lambda: object(), priority=PortPriority.PLUGIN, label="a")
    registry.register(spec.name, lambda: object(), priority=PortPriority.PLUGIN, label="b")
    with pytest.raises(AssemblyError, match="ambiguous"):
        registry.resolve()


def test_explicit_wins_over_plugin():
    spec = next(s for s in PORT_SPECS if s.required and not s.pending)
    explicit_marker = object()
    registry = PortRegistry((spec,))
    registry.register(spec.name, lambda: object(), priority=PortPriority.PLUGIN, label="plugin")
    registry.register(spec.name, lambda: explicit_marker, priority=PortPriority.EXPLICIT, label="explicit")
    registry.resolve()
    assert registry.get(spec.name) is explicit_marker


def test_unavailable_capability_raises_ai_capability_unavailable():
    spec = next(s for s in PORT_SPECS if not s.required and not s.pending)
    registry = PortRegistry((spec,))
    registry.resolve()
    proxy = registry.get(spec.name)
    assert isinstance(proxy, UnavailableCapability)
    with pytest.raises(ApiError) as exc_info:
        proxy.anything
    assert exc_info.value.code == "AI_CAPABILITY_UNAVAILABLE"
    assert exc_info.value.status_code == 503


def test_status_reports_resolution_state():
    required_ready = next(s for s in PORT_SPECS if s.required and not s.pending)
    optional_ready = next(s for s in PORT_SPECS if not s.required and not s.pending)
    pending = next(s for s in PORT_SPECS if s.pending)
    registry = PortRegistry((required_ready, optional_ready, pending))
    registry.register(required_ready.name, lambda: object(), label="impl", plugin="p1")
    registry.resolve()
    status = {item["name"]: item for item in registry.status()}
    assert status[required_ready.name]["state"] == PortState.RESOLVED
    assert status[required_ready.name]["available"] is True
    assert status[required_ready.name]["plugin"] == "p1"
    assert status[optional_ready.name]["state"] == PortState.UNAVAILABLE
    assert status[optional_ready.name]["available"] is False
    assert status[pending.name]["state"] == PortState.NOT_PORTED


def test_factory_failure_surfaces_as_assembly_error():
    spec = next(s for s in PORT_SPECS if s.required and not s.pending)

    def broken_factory():
        raise RuntimeError("boom")

    registry = PortRegistry((spec,))
    registry.register(spec.name, broken_factory, label="broken")
    with pytest.raises(AssemblyError, match="factory failed"):
        registry.resolve()
