"""入站 Webhook 的签名校验。

支持按 Webhook 配置适配外部系统：
- 算法：SHA256 / SHA1
- 编码：HEX / BASE64
- 前缀：如 ``sha256=`` / ``v1=`` / 空
- 签名内容模板：必须包含 ``{body}``，可包含 ``{timestamp}``，
  例如 ``{timestamp}.{body}``（默认）、``{body}``（GitHub 风格）、``{timestamp}:{body}``
- 时间窗：模板含 ``{timestamp}`` 时按配置容忍秒数校验，防重放
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

DEFAULT_TOLERANCE_SECONDS = 300
SUPPORTED_ALGORITHMS = {"SHA256": hashlib.sha256, "SHA1": hashlib.sha1}
SUPPORTED_ENCODINGS = {"HEX", "BASE64"}


def build_signed_payload(template: str, timestamp: str, raw_body: bytes) -> bytes:
    """按模板拼接签名原文；body 按字节拼接，避免二进制/编码损耗。"""
    parts = template.split("{body}")
    chunks: list[bytes] = []
    for index, part in enumerate(parts):
        chunks.append(part.replace("{timestamp}", timestamp).encode("utf-8"))
        if index < len(parts) - 1:
            chunks.append(raw_body)
    return b"".join(chunks)


def compute_signature(
    secret: str,
    *,
    timestamp: str,
    raw_body: bytes,
    algorithm: str = "SHA256",
    encoding: str = "HEX",
    prefix: str = "sha256=",
    template: str = "{timestamp}.{body}",
) -> str:
    digestor = SUPPORTED_ALGORITHMS.get(algorithm.upper(), hashlib.sha256)
    payload = build_signed_payload(template, timestamp, raw_body)
    digest = hmac.new(secret.encode("utf-8"), payload, digestor).digest()
    if encoding.upper() == "BASE64":
        value = base64.b64encode(digest).decode("ascii")
    else:
        value = digest.hex()
    return f"{prefix}{value}"


def verify_signature(
    *,
    secret: str,
    timestamp: str,
    signature: str,
    raw_body: bytes,
    algorithm: str = "SHA256",
    encoding: str = "HEX",
    prefix: str = "sha256=",
    template: str = "{timestamp}.{body}",
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    if not secret or not signature:
        return False
    if "{body}" not in template:
        return False
    if "{timestamp}" in template:
        if not timestamp:
            return False
        try:
            sent_at = int(timestamp)
        except (TypeError, ValueError):
            return False
        if tolerance_seconds > 0 and abs(time.time() - sent_at) > tolerance_seconds:
            return False
    expected = compute_signature(
        secret,
        timestamp=timestamp,
        raw_body=raw_body,
        algorithm=algorithm,
        encoding=encoding,
        prefix=prefix,
        template=template,
    )
    if encoding.upper() == "HEX":
        # hex 大小写不改变语义，统一小写后恒定时间比较
        return hmac.compare_digest(signature.strip().lower(), expected.lower())
    return hmac.compare_digest(signature.strip(), expected)


def verify_token(secret: str, value: str | None, prefix: str = "") -> bool:
    if not secret or not value:
        return False
    text = value.strip()
    if prefix:
        if not text.startswith(prefix):
            return False
        text = text[len(prefix) :].strip()
    return bool(text) and hmac.compare_digest(text, secret)
