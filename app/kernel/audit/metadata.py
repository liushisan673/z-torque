"""审计 detail 写入侧 metadata-only 归一化。

在 `record_audit` 单一入口收口：只允许白名单/安全后缀的元数据键进入
`zr_audit_log.detail`；正文、密钥、工具入参、diff 原文等一律丢弃。
usage 统一走 ModelUsage 归一化，缺失 token 写 "unknown"，不写 0。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from app.core.redaction import is_sensitive_key
from app.kernel.model_gateway.contracts import normalize_usage, parse_model_usage

UNKNOWN = "unknown"
_MAX_STR_CHARS = 200
_MAX_LIST_ITEMS = 50
_MAX_DICT_ITEMS = 40
_MAX_DETAIL_CHARS = 2000

_ALLOWED_KEYS = frozenset(
    {
        "status", "code", "error", "error_code", "error_message", "reason", "stream",
        "finish_reason", "tools", "tool_call_count", "task_created", "usage",
        "provider_request_id", "model", "duration_ms", "compaction_seq",
        "state", "doc_id", "chunks", "size_bytes", "query_hash", "hits",
        "chunk_ids", "indexed", "fields", "agent_id", "auth_mode", "filename",
        "fingerprint", "bridge_path", "owner_id", "created", "skipped", "pages",
        "names", "permission", "requested_actor", "depth", "principal_type",
        "caller_peer_id", "peer", "artifact_id", "hash", "path", "size", "key",
        "added", "removed", "changed", "tool_count", "tool_id", "tool_version_id",
        "version_number", "updated", "scope", "immutable", "version", "source_ref",
        "expected_version", "expected_hash", "actual_version", "actual_hash",
        "format", "count", "shard_count", "schedule_hours", "actor",
        "scanned", "conflicts_removed", "merged", "json_path", "did_you_mean",
        # Phase 2 凭据审计：只记渠道与长度/引用 hash，绝不记明文或密文
        "channel", "value_length",
    }
)

# 明确禁止的正文/凭据类键；即使安全后缀规则命中也不得进入审计。
_DENIED_KEYS = frozenset(
    {
        "prompt", "messages", "content", "text", "arguments", "body",
        "extracted_text", "before_text", "after_text", "api_key", "token",
        "secret", "password", "authorization", "credential", "credentials",
        "cookie", "cookies", "session_id", "access_token", "refresh_token",
        "auth_token", "query", "answer", "payload", "data", "raw", "diff",
    }
)

_SAFE_SUFFIXES = (
    "_id", "_ids", "_hash", "_count", "_number", "_bytes", "_ms", "_seconds",
    "_mode", "_type", "_state", "_status", "_ref", "_at",
)


def _allowed_key(key: str) -> bool:
    normalized = key.strip()
    if not normalized or normalized in _DENIED_KEYS or is_sensitive_key(normalized):
        return False
    if normalized in _ALLOWED_KEYS:
        return True
    return normalized.endswith(_SAFE_SUFFIXES)


def _truncate(text: str) -> str:
    return text if len(text) <= _MAX_STR_CHARS else text[:_MAX_STR_CHARS] + "…[TRUNCATED]"


def _normalize_usage_detail(value: Any) -> dict[str, int | str]:
    if not isinstance(value, dict):
        return normalize_usage(None)
    return normalize_usage(parse_model_usage(value))


def _clean_value(value: Any, *, depth: int) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        return _truncate(value)
    if depth >= 3:
        return UNKNOWN
    if isinstance(value, (list, tuple)):
        return [_clean_value(item, depth=depth + 1) for item in list(value)[:_MAX_LIST_ITEMS]]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in list(value.items())[:_MAX_DICT_ITEMS]:
            key_text = str(key)
            if not _allowed_key(key_text):
                continue
            if key_text == "usage":
                cleaned[key_text] = _normalize_usage_detail(item)
                continue
            result = _clean_value(item, depth=depth + 1)
            if result is not None:
                cleaned[key_text] = result
        return cleaned
    return UNKNOWN


def normalize_audit_detail(detail: dict[str, Any] | None) -> dict[str, Any] | None:
    """收敛任意审计 detail 为 metadata-only；空 detail 返回 None（保持旧语义）。"""
    if not isinstance(detail, dict) or not detail:
        return None
    cleaned: dict[str, Any] = {}
    for key, value in detail.items():
        key_text = str(key)
        if not _allowed_key(key_text):
            continue
        if key_text == "usage":
            cleaned[key_text] = _normalize_usage_detail(value)
            continue
        result = _clean_value(value, depth=0)
        if result is not None:
            cleaned[key_text] = result
    if not cleaned:
        return None
    encoded = json.dumps(cleaned, ensure_ascii=False, default=str, sort_keys=True)
    if len(encoded) > _MAX_DETAIL_CHARS:
        # 超限只保留标量元数据，杜绝大对象通过审计列膨胀。
        cleaned = {key: value for key, value in cleaned.items() if isinstance(value, (bool, int, float, str))}
        cleaned["detail_truncated"] = True
    return cleaned
