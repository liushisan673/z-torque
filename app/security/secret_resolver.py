"""统一密钥引用解析入口（Phase 2 §四第 7 项：Channels 凭据托管与审计）。

此前 10+ 个调用点各自 `removeprefix("secret://")` 再 `decrypt`，空引用、
解密失败的处理口径不一致。本模块收口三件事：
1. `secret://` 识别与空引用拒绝（fail-closed，不把空串当明文）；
2. 经 `secret.provider` Port 解密，未装配时显式失败；
3. 错误信息不含密文或明文，审计侧只记引用 hash。
"""

from __future__ import annotations

from typing import Any

from app.core.errors import ApiError
from app.kernel.registry.active import try_port

SECRET_URI_PREFIX = "secret://"


def is_secret_ref(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(SECRET_URI_PREFIX)


def secret_ref_of(ciphertext: str) -> str:
    """密文 → 引用（只用于生成审计 hash，不回传明文）。"""
    return f"{SECRET_URI_PREFIX}{ciphertext}"


def resolve_secret(
    value: str | None,
    provider: Any = None,
    *,
    required: bool = False,
    channel: str | None = None,
) -> str | None:
    """解析配置值：`secret://<密文>` 解密；非引用原样返回（存量明文兼容）。

    - `required=True` 时空值也拒绝；
    - 空引用（`secret://`）一律拒绝，不退化为空明文；
    - provider 未显式传入时从已解析的 `secret.provider` Port 获取；
    - 解密失败统一 `AI_SECRET_INVALID_REF`，不回显密文/明文。
    """
    if value is None or value == "":
        if required:
            raise ApiError("AI_SECRET_INVALID_REF", "secret value is empty", 400)
        return value
    if not is_secret_ref(value):
        return value
    ciphertext = value[len(SECRET_URI_PREFIX):]
    if not ciphertext:
        raise ApiError("AI_SECRET_INVALID_REF", "secret reference has empty payload", 400)
    if provider is None:
        provider = try_port("secret.provider")
    if provider is None or not callable(getattr(provider, "decrypt", None)):
        raise ApiError("AI_SECRET_UNAVAILABLE", "secret.provider is not assembled", 503)
    try:
        return provider.decrypt(ciphertext)
    except Exception as exc:  # noqa: BLE001 - 底层异常含密文上下文，不得外泄
        raise ApiError(
            "AI_SECRET_INVALID_REF",
            f"secret reference could not be resolved{f' for channel {channel}' if channel else ''}",
            400,
        ) from exc
