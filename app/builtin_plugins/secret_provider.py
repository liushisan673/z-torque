"""secret.provider Port 的本地加密存储实现（文档 §4.3，G3）。

复用 app/security/secret_store.py 的 Fernet 语义（SECRET_STORE_KEY 对称加密），
以 Port 契约形式提供给装配根与其他插件（如 model.provider 的 secret:// 密钥注入）；
企业版可经 PortRegistry 以 PLUGIN/EXPLICIT 优先级替换为 KMS 后端实现。
"""

from app.builtin_plugins.manifest import PluginManifest
from app.kernel.ports.secret_provider import SecretProvider
from app.kernel.registry.port_registry import PortPriority
from app.security.secret_store import SecretStore


class LocalSecretProvider(SecretProvider):
    def __init__(self) -> None:
        self._store = SecretStore()

    def encrypt(self, value: str) -> str:
        return self._store.encrypt(value)

    def decrypt(self, value: str) -> str:
        return self._store.decrypt(value)


class SecretProviderPlugin:
    manifest = PluginManifest(
        name="secret_provider",
        description="本地 Fernet 加密存储（secret:// 引用解密，密钥来自 SECRET_STORE_KEY）",
        provides=("secret.provider",),
    )

    def contribute(self, sdk) -> None:
        # 装配期对象共享（SDK publish 文档化用途）：model_provider 在 contribute
        # 阶段即需解密 secret:// 密钥，而 Port 实例化要等 resolve()；对象共享 + 装配后
        # requires 校验双保险。企业版 KMS 插件替换实现时同样写入该 key。
        provider = LocalSecretProvider()
        sdk.publish("secret.provider", provider)
        sdk.register_port("secret.provider", lambda: provider, label="builtin_local_fernet_store")
