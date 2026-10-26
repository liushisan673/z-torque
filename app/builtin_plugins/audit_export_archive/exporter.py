"""导出件序列化与脱敏（v1.3 §5.1 / §5.6）。

脱敏口径（导出后不可回滚，必须比页面更严）：
- detail：复用 `app.core.redaction.redact_mapping`（按键名递归脱敏，深度上限由内核控制）；
- summary：secret:// 密文引用、Bearer 令牌、`key=value` 形式的口令类字段一律 [REDACTED]；
- strict：主体 id（操作者/资源）做 HMAC，密钥本身绝不出现在导出物中。

导出件是外部契约：字段名与列顺序变更需走版本化（见 config.CSV_COLUMNS）。
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from app.builtin_plugins.audit_export_archive.config import CSV_COLUMNS, REDACTION_STRICT
from app.core.redaction import REDACTED, redact_mapping
from app.core.servertime import iso_utc
from app.plugin_sdk import AuditRecord

_SECRET_URI = re.compile(r"secret://[A-Za-z0-9_\-=:/+.]+")
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_CREDENTIAL_KV = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*[^\s,;&]+")


def redact_text(text: str | None) -> str | None:
    """文本脱敏：密文引用 / Bearer / 口令键值对。"""
    if not text:
        return text
    cleaned = _SECRET_URI.sub(REDACTED, text)
    cleaned = _BEARER.sub(REDACTED, cleaned)
    return _CREDENTIAL_KV.sub(lambda match: f"{match.group(1)}={REDACTED}", cleaned)


def _iso(value: datetime | None) -> str | None:
    return iso_utc(value) if value else None


def _subject_digest(value: str, key: str) -> str:
    """主体 id 的 HMAC 摘要：同一主体在多次导出中稳定可关联，但不可反推。"""
    return "hmac:" + hmac.new(key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def to_export_row(record: AuditRecord, *, profile: str = "standard", subject_key: str = "") -> dict[str, Any]:
    """单条审计事件 → 导出行（字段稳定、有序）。"""
    actor = record.actor_user_id
    resource = record.resource_id
    if profile == REDACTION_STRICT and subject_key:
        actor = _subject_digest(actor, subject_key) if actor else None
        resource = _subject_digest(resource, subject_key) if resource else None
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "actor_user_id": actor,
        "action": record.action,
        "category": record.category,
        "resource_type": record.resource_type,
        "resource_id": resource,
        "summary": redact_text(record.summary) or "",
        "detail": redact_mapping(record.detail or {}),
        "trace_id": record.trace_id,
        "created_at": _iso(record.created_at),
    }


def encode_jsonl(rows: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n" for row in rows)


def encode_csv(rows: Iterable[dict[str, Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        flat = dict(row)
        detail = flat.get("detail")
        flat["detail"] = json.dumps(detail, ensure_ascii=False, separators=(",", ":")) if detail is not None else ""
        writer.writerow(flat)
    return buffer.getvalue().encode("utf-8")


def encode_shard(rows: Iterable[dict[str, Any]], export_format: str) -> bytes:
    if str(export_format).lower() == "csv":
        return encode_csv(rows)
    return encode_jsonl(rows)


def sha256_total(shard_digests: Iterable[str]) -> str:
    """总摘要：按分片顺序复算（外部校验方可对 manifest 逐片重算后比对）。"""
    digest = hashlib.sha256()
    for item in shard_digests:
        digest.update(bytes.fromhex(item))
    return digest.hexdigest()


def manifest_payload(
    *,
    job_id: str,
    tenant_id: str,
    export_format: str,
    filters: dict[str, Any],
    shards: list[dict[str, Any]],
    row_count: int,
    truncated: bool,
    generated_at: datetime,
    recursive: bool = False,
    key_id: str | None = None,
) -> bytes:
    """导出清单：外部消费者可据此校验分片完整性（sha256 + 行数）。

    `key_id` 仅加密冷副本携带（派生密钥的指纹，不可反推密钥）：轮换脚本据此判断
    "这批对象是否仍依赖上一把密钥"。导出明文件不带该字段，字段集保持稳定。
    """
    payload = {
        "schema": "z-torque.audit-export.v1",
        "job_id": job_id,
        "tenant_id": tenant_id,
        "format": export_format,
        "filters": filters,
        "row_count": row_count,
        "truncated": truncated,
        "recursive": recursive,
        "shards": shards,
        "sha256_total": sha256_total([item["sha256"] for item in shards if item.get("sha256")]),
        "generated_at": _iso(generated_at),
    }
    if key_id:
        payload["key_id"] = key_id
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
