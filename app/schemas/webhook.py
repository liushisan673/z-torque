import re
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.services.inbound_webhook import is_valid_path

_HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_PLACEHOLDERS = ("{timestamp}", "{body}")


def _validate_path(value: str) -> str:
    text = (value or "").strip()
    if not is_valid_path(text):
        raise ValueError("path must contain only letters, digits, '.', '_', '[' and ']'")
    return text


def _validate_optional_path(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    return _validate_path(text)


def _validate_placeholders(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if len(value) > 20:
        raise ValueError("at most 20 placeholder texts are allowed")
    return [str(item).strip()[:32] for item in value if str(item).strip()]


def _validate_header(value: str) -> str:
    text = (value or "").strip()
    if not _HEADER_RE.fullmatch(text):
        raise ValueError("header name must contain only letters, digits and '-'")
    return text


def _validate_template(value: str) -> str:
    text = (value or "").strip()
    if "{body}" not in text:
        raise ValueError("signature template must contain {body}")
    if len(text) > 128:
        raise ValueError("signature template is too long")
    # 只允许 {timestamp} / {body} 两个占位符，避免出现不可预期的花括号内容
    remainder = text
    for token in _PLACEHOLDERS:
        remainder = remainder.replace(token, "")
    if "{" in remainder or "}" in remainder:
        raise ValueError("signature template only supports {timestamp} and {body}")
    return text


def _validate_reply_url(value: str | None) -> str | None:
    """回写地址只允许 http(s)，空串视为关闭回写。"""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("reply url must be an absolute http(s) URL")
    return text


class WebhookCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    target_agent_id: UUID
    auth_mode: Literal["HMAC", "TOKEN"] = "HMAC"
    signature_header: str = "X-Webhook-Signature"
    timestamp_header: str = "X-Webhook-Timestamp"
    event_id_header: str = "X-Webhook-Event-Id"
    token_header: str = "Authorization"
    token_prefix: str = Field(default="Bearer ", max_length=32)
    signature_algorithm: Literal["SHA256", "SHA1"] = "SHA256"
    signature_encoding: Literal["HEX", "BASE64"] = "HEX"
    signature_prefix: str = Field(default="sha256=", max_length=32)
    signature_template: str = "{timestamp}.{body}"
    timestamp_tolerance_seconds: int = Field(default=300, ge=0, le=86400)
    message_path: str = "text"
    conversation_key_path: str = "conversation_key"
    event_id_path: str = "event_id"
    # 媒体映射：media_path 为空表示不解析媒体
    media_path: str | None = None
    media_type_path: str = "type"
    media_source_path: str = "filePath"
    media_mime_path: str = "mimeType"
    media_file_name_path: str = "file_name"
    media_placeholder_texts: list[str] | None = None
    # 可选回写：任务终态后把最终答复 POST 回外部系统
    reply_url: str | None = Field(default=None, max_length=512)
    reply_token: str | None = Field(default=None, max_length=512)
    reply_account_path: str = "account_id"
    reply_peer_path: str = "peer_id"
    enabled: bool = True

    @field_validator("message_path", "conversation_key_path", "event_id_path", "reply_account_path", "reply_peer_path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return _validate_path(value)

    @field_validator("media_path")
    @classmethod
    def _check_media_path(cls, value: str | None) -> str | None:
        return _validate_optional_path(value)

    @field_validator("media_type_path", "media_source_path", "media_mime_path", "media_file_name_path")
    @classmethod
    def _check_media_sub_path(cls, value: str) -> str:
        return _validate_path(value)

    @field_validator("media_placeholder_texts")
    @classmethod
    def _check_placeholders(cls, value: list[str] | None) -> list[str] | None:
        return _validate_placeholders(value)

    @field_validator("reply_url")
    @classmethod
    def _check_reply_url(cls, value: str | None) -> str | None:
        return _validate_reply_url(value)

    @field_validator("signature_header", "timestamp_header", "event_id_header", "token_header")
    @classmethod
    def _check_header(cls, value: str) -> str:
        return _validate_header(value)

    @field_validator("signature_template")
    @classmethod
    def _check_template(cls, value: str) -> str:
        return _validate_template(value)


class WebhookPatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    target_agent_id: UUID | None = None
    auth_mode: Literal["HMAC", "TOKEN"] | None = None
    signature_header: str | None = None
    timestamp_header: str | None = None
    event_id_header: str | None = None
    token_header: str | None = None
    token_prefix: str | None = Field(default=None, max_length=32)
    signature_algorithm: Literal["SHA256", "SHA1"] | None = None
    signature_encoding: Literal["HEX", "BASE64"] | None = None
    signature_prefix: str | None = Field(default=None, max_length=32)
    signature_template: str | None = None
    timestamp_tolerance_seconds: int | None = Field(default=None, ge=0, le=86400)
    message_path: str | None = None
    conversation_key_path: str | None = None
    event_id_path: str | None = None
    media_path: str | None = None
    media_type_path: str | None = None
    media_source_path: str | None = None
    media_mime_path: str | None = None
    media_file_name_path: str | None = None
    media_placeholder_texts: list[str] | None = None
    reply_url: str | None = Field(default=None, max_length=512)
    reply_token: str | None = Field(default=None, max_length=512)
    reply_account_path: str | None = None
    reply_peer_path: str | None = None
    enabled: bool | None = None

    @field_validator("message_path", "conversation_key_path", "event_id_path", "reply_account_path", "reply_peer_path")
    @classmethod
    def _check_path(cls, value: str | None) -> str | None:
        return None if value is None else _validate_path(value)

    @field_validator("media_path")
    @classmethod
    def _check_media_path(cls, value: str | None) -> str | None:
        return _validate_optional_path(value)

    @field_validator("media_type_path", "media_source_path", "media_mime_path", "media_file_name_path")
    @classmethod
    def _check_media_sub_path(cls, value: str | None) -> str | None:
        return None if value is None else _validate_path(value)

    @field_validator("media_placeholder_texts")
    @classmethod
    def _check_placeholders(cls, value: list[str] | None) -> list[str] | None:
        return _validate_placeholders(value)

    @field_validator("reply_url")
    @classmethod
    def _check_reply_url(cls, value: str | None) -> str | None:
        return _validate_reply_url(value)

    @field_validator("signature_header", "timestamp_header", "event_id_header", "token_header")
    @classmethod
    def _check_header(cls, value: str | None) -> str | None:
        return None if value is None else _validate_header(value)

    @field_validator("signature_template")
    @classmethod
    def _check_template(cls, value: str | None) -> str | None:
        return None if value is None else _validate_template(value)
