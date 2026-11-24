"""secret.provider Port 与 ModelProviderPlugin 密钥注入（G3）单元测试。

覆盖：Fernet 加解密回环、secret:// 引用解密注入、明文 env 向后兼容、
依赖缺失 fail-fast、community 装配解析。
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from app.assembly import assemble
from app.builtin_plugins.manifest import PluginSdk
from app.builtin_plugins.model_gateway import (
    ModelRoutingGateway,
    OpenAICompatibleGateway,
)
from app.builtin_plugins.plugins import ModelProviderPlugin
from app.builtin_plugins.secret_provider import LocalSecretProvider, SecretProviderPlugin
from app.kernel.plugin_guard import GuardedModelGateway
from app.kernel.ports.specs import PORT_SPECS
from app.kernel.registry.port_registry import AssemblyError, PortRegistry, PortState
from app.security.secret_store import SecretStore


def _specs(*names: str) -> tuple:
    return tuple(s for s in PORT_SPECS if s.name in names)


def _contribute_model_provider(settings, registry):
    objects: dict = {}
    SecretProviderPlugin().contribute(PluginSdk.of(SecretProviderPlugin(), registry=registry, objects=objects))
    ModelProviderPlugin().contribute(PluginSdk.of(ModelProviderPlugin(), settings=settings, registry=registry, objects=objects))


def test_local_secret_provider_roundtrip():
    provider = LocalSecretProvider()
    ciphertext = provider.encrypt("sk-test-123")
    assert ciphertext != "sk-test-123"
    assert provider.decrypt(ciphertext) == "sk-test-123"


def test_model_provider_resolves_secret_uri_key():
    # G3 主路径：API Key 以 secret:// 引用配置，装配期经 secret.provider 解密注入
    encrypted = SecretStore().encrypt("sk-real-key")
    registry = PortRegistry(_specs("secret.provider", "model.provider"))
    settings = SimpleNamespace(model_base_url="https://api.example.com/v1", model_api_key=f"secret://{encrypted}", model_name="gpt-x", model_allowed_names=[])
    _contribute_model_provider(settings, registry)
    registry.resolve()
    gateway = registry.get("model.provider")
    assert isinstance(gateway, GuardedModelGateway)
    assert isinstance(gateway._inner, ModelRoutingGateway)
    default = gateway._inner._default_gateway
    assert isinstance(default, OpenAICompatibleGateway)
    assert default.api_key == "sk-real-key"


def test_model_provider_plain_key_backward_compatible():
    # 向后兼容：明文 env 配置继续可用（不强制存量部署迁移）
    registry = PortRegistry(_specs("secret.provider", "model.provider"))
    settings = SimpleNamespace(model_base_url="https://api.example.com/v1", model_api_key="sk-plain", model_name="gpt-x", model_allowed_names=[])
    _contribute_model_provider(settings, registry)
    registry.resolve()
    default = registry.get("model.provider")._inner._default_gateway
    assert isinstance(default, OpenAICompatibleGateway)
    assert default.api_key == "sk-plain"


def test_model_provider_without_key_has_no_mock_fallback():
    """决策（阶段13）：未配置模型时不再降级为 MockModelGateway。

    原实现让全新部署"看起来能对话"却只返回 [MOCK] 假回复，掩盖了"必须先配模型"。
    现在 default_gateway 为 None，对话会显式报 AI_MODEL_NOT_CONFIGURED。
    Mock 仅由测试注入（tests/conftest.py），生产装配不再创建。
    """
    registry = PortRegistry(_specs("secret.provider", "model.provider"))
    settings = SimpleNamespace(model_base_url=None, model_api_key=None, model_name="mock", model_allowed_names=[])
    _contribute_model_provider(settings, registry)
    registry.resolve()
    gateway = registry.get("model.provider")
    assert isinstance(gateway, GuardedModelGateway)
    assert isinstance(gateway._inner, ModelRoutingGateway)
    assert gateway._inner._default_gateway is None
    # 无全局模型时也不应对外声称有默认模型（否则又会变成隐式兜底）
    assert gateway._inner.default_model == ""


def test_model_provider_requires_secret_provider_fail_fast():
    # 依赖缺失：未加载 secret_provider 时 model_provider 贡献即失败（明确加载顺序提示）
    registry = PortRegistry(_specs("model.provider"))
    settings = SimpleNamespace(model_base_url="https://api.example.com/v1", model_api_key="sk-plain", model_name="gpt-x", model_allowed_names=[])
    with pytest.raises(AssemblyError, match="secret.provider"):
        ModelProviderPlugin().contribute(PluginSdk.of(ModelProviderPlugin(), settings=settings, registry=registry, objects={}))


def test_community_assembly_resolves_secret_and_model_ports(tmp_path):
    settings = SimpleNamespace(
        model_base_url=None,
        model_api_key=None,
        model_name="mock",
        model_allowed_names=[],
        file_storage_dir=tmp_path / "files",
        # v1.3：沙箱 cwd 白名单在装配期强制校验，轻量 settings 必须显式给出
        sandbox_allowed_roots=[str(tmp_path / "skills")],
    )
    result = assemble(settings, create_engine("sqlite://"))
    assert result.registry.resolution("secret.provider").state == PortState.RESOLVED
    assert result.registry.resolution("model.provider").state == PortState.RESOLVED
