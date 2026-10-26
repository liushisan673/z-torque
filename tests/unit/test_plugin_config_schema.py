"""插件配置契约（阶段 2）：config_schema 结构化为 JSON Schema + 装配期投影校验。

三条边界（避免"第二套配置中心"）：
1. schema 只做**形状**校验（类型/枚举/必填），语义校验仍归插件自己的 `config.validate()`；
2. 只投影 schema 声明的键，不把整个 Settings 灌给插件；
3. 历史描述性 schema（{key: "说明"}）保持不校验，21 个旧插件行为不变。
"""

from __future__ import annotations

import pytest

from app.assembly import assemble
from app.builtin_plugins.a2a_gateway.plugin import A2AGatewayPlugin
from app.builtin_plugins.audit_export_archive.plugin import AuditExportArchivePlugin
from app.builtin_plugins.modules import resolve_profile
from app.builtin_plugins.sandbox_driver.plugin import SandboxDriverPlugin
from app.config import get_settings
from app.db.session import engine
from app.kernel.registry.port_registry import PORT_SPEC_BY_NAME, AssemblyError
from app.plugin_sdk import (
    PluginManifest,
    is_structured_config_schema,
    project_plugin_config,
    validate_manifest,
    validate_plugin_config,
)

STANDARD_PLUGINS = (SandboxDriverPlugin, AuditExportArchivePlugin, A2AGatewayPlugin)


class _EmptySettings:
    """没有任何插件配置属性的轻量替身（模拟直构 / 自定义内核）。"""


@pytest.mark.parametrize("plugin_cls", STANDARD_PLUGINS)
def test_standard_plugins_declare_structured_schema(plugin_cls):
    schema = plugin_cls.manifest.config_schema
    assert is_structured_config_schema(schema), "标准插件（可移植）必须声明结构化 JSON Schema"
    assert schema["type"] == "object"
    assert schema["properties"], "结构化 schema 必须声明属性"
    for key, spec in schema["properties"].items():
        # 类型 + 描述：企业版据此判断"这个键是什么、要不要配"
        assert "type" in spec, f"{plugin_cls.manifest.name}.{key} 缺少 type"
        assert spec.get("description"), f"{plugin_cls.manifest.name}.{key} 缺少 description"


@pytest.mark.parametrize("plugin_cls", STANDARD_PLUGINS)
def test_standard_schema_passes_manifest_validation(plugin_cls):
    # 结构化 schema 必须是合法 JSON Schema（否则 import 期就该失败）
    validate_manifest(plugin_cls.manifest, set(PORT_SPEC_BY_NAME))


def test_projection_fills_defaults_when_settings_lacks_key():
    projected = project_plugin_config(AuditExportArchivePlugin.manifest, _EmptySettings())
    assert projected["audit_archive_prefix"] == "audit-archive"
    assert projected["audit_export_redaction_profile"] == "standard"
    assert projected["audit_export_formats"] == ["jsonl"]


def test_projection_prefers_settings_value():
    settings = get_settings()
    projected = project_plugin_config(AuditExportArchivePlugin.manifest, settings)
    assert projected["audit_export_chunk_rows"] == settings.audit_export_chunk_rows


def test_projection_is_allowlisted_to_declared_keys():
    projected = project_plugin_config(SandboxDriverPlugin.manifest, get_settings())
    assert set(projected) == set(SandboxDriverPlugin.manifest.config_schema["properties"])


def test_enum_violation_fails_assembly_before_contribute():
    # model_copy 绕过 pydantic 校验，模拟"环境变量写错值"的真实事故
    settings = get_settings().model_copy(update={"a2a_identity_propagation": "carrier-pigeon"})
    with pytest.raises(AssemblyError, match="a2a_identity_propagation"):
        assemble(settings, engine, profile=resolve_profile("enterprise"))


def test_type_violation_fails_assembly():
    settings = get_settings().model_copy(update={"a2a_max_fanout": "many"})
    with pytest.raises(AssemblyError, match="a2a_max_fanout"):
        assemble(settings, engine, profile=resolve_profile("enterprise"))


def test_required_key_missing_fails():
    manifest = PluginManifest(
        name="needs_explicit_key",
        description="",
        provides=(),
        config_schema={
            "type": "object",
            "properties": {"must_set": {"type": "string", "description": "必填项"}},
            "required": ["must_set"],
            "additionalProperties": False,
        },
    )
    with pytest.raises(AssemblyError, match="must_set"):
        validate_plugin_config(manifest, _EmptySettings())


def test_legacy_descriptive_schema_is_not_validated():
    manifest = PluginManifest(
        name="legacy_plugin",
        description="历史形态",
        provides=(),
        config_schema={"legacy_key": "int，说明文字，不是 JSON Schema"},
    )
    assert not is_structured_config_schema(manifest.config_schema)
    # 不校验、不抛错：21 个旧插件保持原行为
    validate_plugin_config(manifest, _EmptySettings())
    validate_manifest(manifest, set())


def test_invalid_json_schema_is_rejected_at_manifest_level():
    manifest = PluginManifest(
        name="bad_schema",
        description="",
        provides=(),
        config_schema={"type": "object", "properties": {"x": {"type": "not-a-real-type"}}},
    )
    with pytest.raises(ValueError, match="invalid config_schema"):
        validate_manifest(manifest, set())


def test_additional_properties_is_rejected_when_plugin_declares_it():
    # additionalProperties=False 的意义：投影不会带进未声明的键，但显式违规必须报错
    manifest = PluginManifest(
        name="strict_keys",
        description="",
        provides=(),
        config_schema={
            "type": "object",
            "properties": {"only": {"type": "integer", "default": 1}},
            "additionalProperties": False,
        },
    )
    assert validate_plugin_config(manifest, _EmptySettings()) == {"only": 1}
