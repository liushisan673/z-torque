"""统一的敏感信息脱敏工具（执行日志入参、审计 detail 等结构化数据共用）。

字符串摘要的脱敏见 app.kernel.task.service.sanitize_summary；本模块处理 dict/list，
按键名判定敏感字段并把值整体替换，避免把 DB 口令、HTTP 密钥等原始入参落库。
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# 以 "_" 分词后精确命中的敏感词（不用 "token" 做子串匹配，避免误伤 input_tokens / max_tokens）
_SENSITIVE_PARTS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "authorization",
    "credential",
    "credentials",
    "cookie",
    "cookies",
}
# 与 "_" 分词无法区分、需要整词匹配的复合名
_SENSITIVE_COMPOUND = (
    "apikey",
    "accesskey",
    "privatekey",
    "clientsecret",
    "sessionid",
    "accesstoken",
    "refreshtoken",
    "authtoken",
)
_MAX_DEPTH = 6


def is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if not normalized:
        return False
    if _SENSITIVE_PARTS.intersection(normalized.split("_")):
        return True
    joined = normalized.replace("_", "")
    return any(compound in joined for compound in _SENSITIVE_COMPOUND)


def redact_mapping(value: Any, *, _depth: int = 0) -> Any:
    """递归脱敏 dict/list 中的敏感字段；超深结构直接整体替换，避免递归放大。"""
    if _depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        return {
            key: (REDACTED if is_sensitive_key(str(key)) else redact_mapping(item, _depth=_depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_mapping(item, _depth=_depth + 1) for item in value]
    return value
