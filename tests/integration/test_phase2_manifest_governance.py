"""Phase 2 阶段一：manifest-first 校验 + capability ladder 真实入口验收。

负向先行：能力（Port 注册 / 共享服务发布）未声明必须在执行点被拒绝；
core_change 无审批引用必须在装配期被拒绝。正向验证四级阶梯与真实
/capabilities 上报。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.builtin_plugins.plugins import BUILTIN_PLUGINS
from app.kernel.ports.specs import PORT_SPEC_BY_NAME
from app.kernel.registry.port_registry import AssemblyError
from app.main import app
from app.plugin_sdk import (
    CapabilityDeclaration,
    CapabilityLevel,
    PluginManifest,
    PluginSdk,
    summarize_governance,
    validate_manifest,
)


class _FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def register(self, port_name, factory, *, priority, label, plugin=None) -> None:
        self.calls.append(port_name)


def test_register_port_requires_manifest_declaration():
    """负向：provides 未声明的 Port 不得注册。"""
    manifest = PluginManifest(name="undeclared", description="", provides=())
    sdk = PluginSdk(registry=_FakeRegistry(), manifest=manifest)
    with pytest.raises(AssemblyError, match="undeclared port"):
        sdk.register_port("memory.long", lambda: None, label="x")


def test_publish_requires_manifest_declaration():
    """负向：领域服务未在 published_services 声明不得发布。"""
    manifest = PluginManifest(name="undeclared", description="", provides=("memory.long",))
    sdk = PluginSdk(objects={}, manifest=manifest)
    with pytest.raises(AssemblyError, match="undeclared capability"):
        sdk.publish("mcp_lifecycle", object())


def test_manifest_first_allows_declared_capabilities():
    """正向：声明后的 Port 与领域服务可正常注册/发布。"""
    manifest = PluginManifest(
        name="declared",
        description="",
        provides=("memory.long",),
        published_services=("my_service",),
        capabilities=(CapabilityDeclaration("my_service", CapabilityLevel.EXTEND_EXISTING),),
    )
    registry = _FakeRegistry()
    sdk = PluginSdk(registry=registry, objects={}, manifest=manifest)
    sdk.register_port("memory.long", lambda: None, label="ok")
    sdk.publish("my_service", object())
    assert registry.calls == ["memory.long"]
    assert sdk.shared("my_service") is not None


def test_capability_ladder_core_change_requires_approval():
    """负向/正向：core_change 必须有审批引用，带审批可通过 manifest 校验。"""
    with pytest.raises(AssemblyError, match="approval_ref"):
        CapabilityDeclaration("brand_new_port", CapabilityLevel.CORE_CHANGE)
    approved = CapabilityDeclaration(
        "brand_new_port", CapabilityLevel.CORE_CHANGE, "new kernel port", approval_ref="ADR-001"
    )
    validate_manifest(
        PluginManifest(name="core", description="", provides=(), capabilities=(approved,)),
        set(PORT_SPEC_BY_NAME),
    )


def test_capability_ladder_rejects_unknown_level_and_duplicates():
    with pytest.raises(AssemblyError, match="unknown ladder level"):
        CapabilityDeclaration("x", "made_up_level")
    with pytest.raises(AssemblyError, match="duplicate capability"):
        validate_manifest(
            PluginManifest(
                name="dup",
                description="",
                provides=(),
                capabilities=(
                    CapabilityDeclaration("x", CapabilityLevel.EXTEND_EXISTING),
                    CapabilityDeclaration("x", CapabilityLevel.EXISTING_CONTRACT),
                ),
            ),
            set(),
        )


def test_production_manifests_satisfy_manifest_first_and_expose_ladder():
    """生产 22 插件全部通过门禁；四级阶梯摘要可汇总。"""
    manifests = tuple(cls.manifest for cls in BUILTIN_PLUGINS)
    for manifest in manifests:
        validate_manifest(manifest, set(PORT_SPEC_BY_NAME))
    governance = summarize_governance(manifests)
    assert governance["manifest_first"] is True
    assert set(governance["levels"]) == {
        "extend_existing", "existing_contract", "narrow_extension", "core_change"
    }
    assert governance["levels"]["extend_existing"] >= 1
    assert governance["levels"]["existing_contract"] >= 1
    assert governance["levels"]["narrow_extension"] >= 2
    assert governance["declarations"]["a2a.dispatch"]["plugin"] == "a2a_gateway"
    assert governance["shared_capabilities"] == []


@pytest.mark.asyncio
async def test_capabilities_api_reports_governance_without_breaking_legacy_fields():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/z-torque/capabilities")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    for legacy in ("stage", "ports", "mcp", "knowledge", "memory", "capability_cards"):
        assert legacy in data
    governance = data["governance"]
    assert governance["manifest_first"] is True
    assert governance["declarations"]["mcp.lifecycle"]["level"] == "narrow_extension"
    assert governance["declarations"]["audit.export"]["level"] == "existing_contract"
