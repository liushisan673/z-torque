"""Usage 归一化与按窗口汇总写入。

上游未报告的 token 分类一律写「未知」：审计出口用字符串 `unknown`，
DB 汇总列用 NULL；二者都不伪装成 0。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel.model_gateway.contracts import UNKNOWN_TOKEN_VALUE, ModelUsage, normalize_usage
from app.models import UsageSummary

_COLUMN_BY_KEY = {
    "prompt_tokens": "token_in",
    "completion_tokens": "token_out",
    "cache_read_tokens": "cache_read",
    "cache_write_tokens": "cache_write",
}


def _numeric(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _scope_condition(tenant_id: UUID, agent_id: UUID | None, model: str, window: str):
    agent_clause = UsageSummary.agent_id.is_(None) if agent_id is None else UsageSummary.agent_id == agent_id
    return (
        UsageSummary.tenant_id == tenant_id,
        agent_clause,
        UsageSummary.model == model,
        UsageSummary.window == window,
    )


def _serialize_value(value: int | None) -> int | str:
    return UNKNOWN_TOKEN_VALUE if value is None else value


def record_usage_summary(
    db: Session,
    *,
    tenant_id: UUID,
    agent_id: UUID | None,
    model: str,
    usage: ModelUsage | None,
    window: str | None = None,
) -> UsageSummary:
    """累加一次调用的 token；未知字段会污染该窗口对应字段为未知。"""
    normalized = normalize_usage(usage)
    model = (model or "unknown").strip()[:128] or "unknown"
    window = (window or datetime.now(timezone.utc).strftime("%Y-%m-%d")).strip()[:32]
    row = db.scalar(select(UsageSummary).where(*_scope_condition(tenant_id, agent_id, model, window)))
    if row is None:
        row = UsageSummary(tenant_id=tenant_id, agent_id=agent_id, model=model, window=window)
        db.add(row)
        for key, column in _COLUMN_BY_KEY.items():
            setattr(row, column, _numeric(normalized[key]))
        db.flush()
        return row
    for key, column in _COLUMN_BY_KEY.items():
        incoming = _numeric(normalized[key])
        current = getattr(row, column)
        if incoming is None or current is None:
            setattr(row, column, None)
        else:
            setattr(row, column, current + incoming)
    db.flush()
    return row


def serialize_usage_summary(row: UsageSummary) -> dict[str, Any]:
    """API 出口：未知写 `unknown`，不写 0。"""
    return {
        "id": str(row.id),
        "agent_id": str(row.agent_id) if row.agent_id else None,
        "model": row.model,
        "window": row.window,
        "token_in": _serialize_value(row.token_in),
        "token_out": _serialize_value(row.token_out),
        "cache_read": _serialize_value(row.cache_read),
        "cache_write": _serialize_value(row.cache_write),
    }
