"""入站媒体获取：本地路径 / http(s) / base64。

安全约束：
- 本地路径只能落在配置的媒体根目录内，拒绝 ``..``、绝对路径逃逸与符号链接逃逸；
- http(s) 拒绝回环、内网、链路本地与云元数据地址，禁止跟随重定向；
- base64 与 HTTP 响应都受单文件大小上限约束。
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.core.errors import ApiError

_BLOCKED_HOSTS = {"169.254.169.254", "metadata.google.internal", "metadata", "fd00:ec2::254"}


def _error(message: str, *, status_code: int, code: str = "AI_PARAM_INVALID") -> ApiError:
    return ApiError(code, message, status_code)


def _blocked_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast


def _assert_public_host(host: str) -> None:
    if not host or host.lower() in _BLOCKED_HOSTS:
        raise _error("media host is not allowed", status_code=400)
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise _error("media host cannot be resolved", status_code=400) from exc
    for info in infos:
        if _blocked_ip(info[4][0]):
            raise _error("media host resolves to a private address", status_code=400)


def _resolve_local(source: str, roots: list[Path]) -> Path:
    if not roots:
        raise _error("local media roots are not configured", status_code=400)
    target = Path(source).expanduser().resolve()
    for root in roots:
        base = root.expanduser().resolve()
        if target == base or base in target.parents:
            return target
    raise _error("media path escapes configured roots", status_code=400)


async def _fetch_http(source: str, max_bytes: int, timeout: float) -> bytes:
    parsed = urlparse(source)
    if parsed.scheme not in {"http", "https"}:
        raise _error("unsupported media url scheme", status_code=400)
    if parsed.username or parsed.password:
        raise _error("media url must not embed credentials", status_code=400)
    _assert_public_host(parsed.hostname or "")
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream("GET", source) as response:
                response.raise_for_status()
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        raise _error("media exceeds configured limit", status_code=413, code="AI_FILE_TOO_LARGE")
                return bytes(buffer)
    except httpx.HTTPError as exc:
        raise _error("media download failed", status_code=400) from exc


async def fetch_media(
    source: str,
    *,
    roots: list[Path],
    max_bytes: int,
    timeout: float = 15.0,
) -> bytes:
    """按本地路径 / http(s) / base64 获取媒体字节；失败抛 ``ApiError``。"""
    value = (source or "").strip()
    if not value:
        raise _error("media source is empty", status_code=400)
    if value.startswith("base64:"):
        try:
            data = base64.b64decode(value[len("base64:"):], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _error("media base64 is invalid", status_code=400) from exc
        if len(data) > max_bytes:
            raise _error("media exceeds configured limit", status_code=413, code="AI_FILE_TOO_LARGE")
        return data
    if value.startswith(("http://", "https://")):
        return await _fetch_http(value, max_bytes, timeout)
    target = _resolve_local(value, roots)
    if not target.is_file():
        raise _error("media file does not exist", status_code=400)
    if target.stat().st_size > max_bytes:
        raise _error("media exceeds configured limit", status_code=413, code="AI_FILE_TOO_LARGE")
    return target.read_bytes()
