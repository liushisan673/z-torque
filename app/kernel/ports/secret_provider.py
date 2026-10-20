from abc import ABC, abstractmethod


class SecretProvider(ABC):
    """secret.provider Port 契约：密钥加密存储与解密（文档 §4.3）。

    插件配置中的敏感值使用 `secret://<密文>` 引用形态，由实现负责解密；
    具体实现属能力实现，社区版内置 Fernet 本地加密存储
    （app/builtin_plugins/secret_provider.py），企业版可替换为 KMS 后端。
    """

    @abstractmethod
    def encrypt(self, value: str) -> str:
        """明文 → 密文（返回值供调用方拼装 secret:// 引用）。"""

    @abstractmethod
    def decrypt(self, value: str) -> str:
        """密文 → 明文；解密失败抛实现侧异常（调用方按 AI_CAPABILITY_UNAVAILABLE 等语义处理）。"""
