"""secret.provider Port 与 ModelProviderPlugin 密钥注入（G3）单元测试。

覆盖：Fernet 加解密回环、secret:// 引用解密注入、明文 env 向后兼容、
依赖缺失 fail-fast、community 装配解析。
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from app.assembly import assemble
from app.builtin_plugins.manifest import PluginSdk
from app.builtin_plugins.model_gateway import MockModelGateway, OpenAICompatibleGateway
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
    assert isinstance(gateway._inner, OpenAICompatibleGateway)
    assert gateway._inner.api_key == "sk-real-key"


def test_model_provider_plain_key_backward_compatible():
    # 向后兼容：明文 env 配置继续可用（不强制存量部署迁移）
    registry = PortRegistry(_specs("secret.provider", "model.provider"))
    settings = SimpleNamespace(model_base_url="https://api.example.com/v1", model_api_key="sk-plain", model_name="gpt-x", model_allowed_names=[])
    _contribute_model_provider(settings, registry)
    registry.resolve()
    assert registry.get("model.provider").api_key == "sk-plain"


def test_model_provider_without_key_falls_back_to_mock():
    registry = PortRegistry(_specs("secret.provider", "model.provider"))
    settings = SimpleNamespace(model_base_url=None, model_api_key=None, model_name="mock", model_allowed_names=[])
    _contribute_model_provider(settings, registry)
    registry.resolve()
    gateway = registry.get("model.provider")
    assert isinstance(gateway, GuardedModelGateway)
    assert isinstance(gateway._inner, MockModelGateway)


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
