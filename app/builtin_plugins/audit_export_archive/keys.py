"""归档冷副本密钥环：当前密钥写、上一把密钥只读（v1.3 §12.9）。

为什么要密钥环
--------------
归档密文自描述（magic `ZAE1` + 12 字节 nonce + AES-256-GCM），但**密文里不内嵌密钥标识**：
密文本身不该携带任何密钥线索。于是"这一坨是谁加密的"只能靠索引/manifest 的 `key_id`，
而"能不能解开"只能靠密钥环按序试。轮换因此拆成两步，永不存在"中间态读不出"的窗口：

1. 登记新密钥并切换写入：`AUDIT_ARCHIVE_SECRET_REF` = 新引用，
   `AUDIT_ARCHIVE_PREVIOUS_SECRET_REF` = 旧引用（只读）；
2. 旧密钥保留只读，直到确认"没有任何对象只能被旧密钥解开"（`--retire-old` 闸门）才移除。

失败语义（刻意不静默）
----------------------
- 写：一律用**当前**密钥；当前密钥缺失/解不开 → `AI_AUDIT_ARCHIVE_FAILED`，绝不降级明文；
- 读：当前 → 上一把依次尝试；全都解不开 → `AI_AUDIT_ARCHIVE_KEY_MISSING`（502），
  绝不返回脏数据、绝不"跳过这条继续";
- 上一把密钥引用配了但解不开 → 装配/触发期即 `AI_AUDIT_ARCHIVE_FAILED`。
  （只读密钥坏了不会让写入降级，但它一旦被用到就是数据不可读，必须当场暴露。）

`key_id` 是派生密钥的指纹（`sha256(key)` 前 16 位十六进制），**不可反推密钥**，
用途只有"索引/manifest 标记归属 + 轮换脚本对账"。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any

from app.core.errors import ApiError

# 密文格式常量：离线校验工具与实现共享，变更即破坏向后兼容
MAGIC = b"ZAE1"
NONCE_BYTES = 12

# 轮换/解包失败专用错误码：旧密钥被 retire 后仍遇到只有它能解的对象
KEY_MISSING = "AI_AUDIT_ARCHIVE_KEY_MISSING"

SECRET_PREFIX = "secret://"

KEY_SOURCE_CURRENT = "current"
KEY_SOURCE_PREVIOUS = "previous"

KEY_ID_BYTES = 16  # 指纹截断长度（十六进制字符数）


def derive_key(secret: str) -> bytes:
    """密钥材料 → 32 字节 AES 密钥（sha256 派生，配置里永远只放密文引用）。"""
    return hashlib.sha256(secret.encode("utf-8")).digest()


def key_id_of(key: bytes) -> str:
    """密钥指纹：可公开写进 manifest / 索引，不可反推密钥。"""
    return hashlib.sha256(key).hexdigest()[:KEY_ID_BYTES]


def encrypt_archive_payload(data: bytes, key: bytes) -> bytes:
    """AES-256-GCM：magic + nonce + ciphertext（含 GCM tag）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(NONCE_BYTES)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, None)


def decrypt_archive_payload(payload: bytes, key: bytes) -> bytes:
    """冷副本解密（供离线校验工具复用）；格式不符直接拒绝。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not payload.startswith(MAGIC) or len(payload) <= len(MAGIC) + NONCE_BYTES:
        raise ValueError("not a z-torque encrypted archive object")
    nonce = payload[len(MAGIC) : len(MAGIC) + NONCE_BYTES]
    return AESGCM(key).decrypt(nonce, payload[len(MAGIC) + NONCE_BYTES :], None)


def is_encrypted_payload(payload: bytes) -> bool:
    return payload.startswith(MAGIC) and len(payload) > len(MAGIC) + NONCE_BYTES


@dataclass(frozen=True)
class ArchiveKey:
    """密钥环里的一把密钥（`key` 只活在内存，绝不落日志/落盘/进 manifest）。"""

    key: bytes
    key_id: str
    source: str

    @classmethod
    def from_secret(cls, secret: str, *, source: str) -> "ArchiveKey":
        material = derive_key(secret)
        return cls(key=material, key_id=key_id_of(material), source=source)


class ArchiveKeyRing:
    """当前密钥 + 上一把密钥（只读）。写入恒定用当前密钥。"""

    def __init__(self, *, current: ArchiveKey, previous: ArchiveKey | None = None) -> None:
        self._current = current
        # 同一把密钥被配成两份（不同引用解出同一密钥）不算轮换，直接塌缩成一份
        self._previous = previous if previous is not None and previous.key_id != current.key_id else None

    @property
    def current(self) -> ArchiveKey:
        return self._current

    @property
    def previous(self) -> ArchiveKey | None:
        return self._previous

    @property
    def write_key(self) -> bytes:
        return self._current.key

    @property
    def write_key_id(self) -> str:
        return self._current.key_id

    def candidates(self) -> tuple[ArchiveKey, ...]:
        return (self._current,) if self._previous is None else (self._current, self._previous)

    def known_key_ids(self) -> tuple[str, ...]:
        return tuple(item.key_id for item in self.candidates())

    def decrypt(self, payload: bytes) -> tuple[bytes, ArchiveKey]:
        """按当前 → 上一把顺序解密；全都失败即 KEY_MISSING（不静默跳过）。"""
        if not is_encrypted_payload(payload):
            raise ValueError("not a z-torque encrypted archive object")
        for candidate in self.candidates():
            try:
                return decrypt_archive_payload(payload, candidate.key), candidate
            except Exception:  # noqa: BLE001 - GCM tag 失败就是"这把不对"，换下一把
                continue
        raise ApiError(
            KEY_MISSING,
            "archive object cannot be decrypted with configured keys "
            f"(tried key_id={','.join(self.known_key_ids())})",
            502,
        )

    def decrypt_with_current(self, payload: bytes) -> bytes:
        """只用当前密钥解密：轮换闸门据此判断"对象是否仍依赖旧密钥"。"""
        if not is_encrypted_payload(payload):
            raise ValueError("not a z-torque encrypted archive object")
        try:
            return decrypt_archive_payload(payload, self._current.key)
        except Exception as exc:  # noqa: BLE001 - 统一转成对外错误码
            raise ApiError(
                KEY_MISSING,
                f"archive object requires a retired key (current key_id={self._current.key_id})",
                502,
            ) from exc


def _resolve_secret(ref: str, provider: Any, *, label: str) -> str:
    clean = (ref or "").strip()
    ciphertext = clean[len(SECRET_PREFIX) :] if clean.startswith(SECRET_PREFIX) else clean
    try:
        plaintext = provider.decrypt(ciphertext)
    except Exception as exc:  # noqa: BLE001 - 解密失败一律按密钥缺失处理，不泄露实现细节
        raise ApiError("AI_AUDIT_ARCHIVE_FAILED", f"audit archive {label} key cannot be decrypted", 502) from exc
    if not plaintext:
        raise ApiError("AI_AUDIT_ARCHIVE_FAILED", f"audit archive {label} key is empty", 502)
    return plaintext


def build_key_ring(
    *,
    enabled: bool,
    secret_ref: str,
    previous_secret_ref: str = "",
    provider: Any = None,
) -> ArchiveKeyRing | None:
    """按配置装配密钥环；未启用加密返回 None（调用方据此走明文路径）。

    所有失败都在**写批次之前**发生（`trigger` 的第一步），因此不会留下永久 FAILED 的空批次。
    """
    if not enabled:
        return None
    clean_ref = (secret_ref or "").strip()
    if not clean_ref:
        raise ApiError("AI_AUDIT_ARCHIVE_FAILED", "AUDIT_ARCHIVE_SECRET_REF is required when archive encryption is enabled", 502)
    if provider is None:
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "secret.provider is not assembled", 503)
    current = ArchiveKey.from_secret(_resolve_secret(clean_ref, provider, label="current"), source=KEY_SOURCE_CURRENT)
    clean_previous = (previous_secret_ref or "").strip()
    if not clean_previous:
        return ArchiveKeyRing(current=current)
    if clean_previous == clean_ref:
        # 配置校验已拦，这里再兜一次：同一引用既当写密钥又当只读密钥，等于没轮换
        raise ApiError("AI_AUDIT_ARCHIVE_FAILED", "audit archive previous key must differ from the current key", 502)
    previous = ArchiveKey.from_secret(
        _resolve_secret(clean_previous, provider, label="previous"), source=KEY_SOURCE_PREVIOUS
    )
    return ArchiveKeyRing(current=current, previous=previous)
