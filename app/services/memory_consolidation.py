"""Dream 式长期记忆整合（P1a-8）。

不新增游标表：游标与 diff 复用现有 `zr_memory_long` 的 key/value/source_ref；
整合后的事实原位替换（mutable 版本链保留旧值），重复事实软删除。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel.audit.service import record_audit
from app.models import LongMemory

CURSOR_PREFIX = "__dream:cursor:"
CONSOLIDATOR_SOURCE = "dream:consolidator"


def _cursor_key(scope: str, agent_id: UUID | None) -> str:
    return f"{CURSOR_PREFIX}{scope}:{agent_id or 'user'}"


def _read_cursor(db: Session, *, tenant_id: UUID, user_id: UUID, scope: str, agent_id: UUID | None) -> tuple[dict, LongMemory | None]:
    row = db.scalar(
        select(LongMemory).where(
            LongMemory.tenant_id == tenant_id,
            LongMemory.user_id == user_id,
            LongMemory.scope == scope,
            LongMemory.key == _cursor_key(scope, agent_id),
            LongMemory.source_ref == CONSOLIDATOR_SOURCE,
            LongMemory.status == "ACTIVE",
        )
    )
    if row is None:
        return {}, None
    try:
        value = json.loads(row.value)
    except (TypeError, ValueError):
        value = {}
    return (value if isinstance(value, dict) else {}), row


class MemoryConsolidationService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def consolidate(self, db: Session, *, tenant_id: UUID, user_id: UUID, scope: str, agent_id: UUID | None, trace_id: str | None = None) -> dict:
        cursor, cursor_row = _read_cursor(db, tenant_id=tenant_id, user_id=user_id, scope=scope, agent_id=agent_id)
        last_at = cursor.get("last_created_at")
        stmt = select(LongMemory).where(
            LongMemory.tenant_id == tenant_id,
            LongMemory.user_id == user_id,
            LongMemory.scope == scope,
            LongMemory.status == "ACTIVE",
            LongMemory.source_ref != CONSOLIDATOR_SOURCE,
        )
        if agent_id is not None:
            stmt = stmt.where(LongMemory.agent_id == agent_id)
        if last_at:
            try:
                threshold = datetime.fromisoformat(str(last_at))
                stmt = stmt.where(LongMemory.created_at > threshold)
            except ValueError:
                pass
        rows = db.scalars(stmt.order_by(LongMemory.created_at, LongMemory.id)).all()
        grouped: dict[str, list[LongMemory]] = {}
        for row in rows:
            grouped.setdefault(" ".join(str(row.value).strip().lower().split()), []).append(row)

        merged: list[str] = []
        conflicts_removed = 0
        for _fingerprint, group in grouped.items():
            if len(group) < 2:
                continue
            canonical = max(group, key=lambda item: (item.created_at, str(item.id)))
            duplicates = [item for item in group if item.id != canonical.id]
            diffs = [{"key": item.key, "value": item.value[:500]} for item in duplicates]
            envelope = json.dumps({
                "fact": canonical.value,
                "diff": {"replaced": diffs, "count": len(diffs)},
                "merged_from": [str(item.id) for item in group],
            }, ensure_ascii=False)
            self.store.remember_scoped(
                db, tenant_id=tenant_id, user_id=user_id, key=canonical.key, value=envelope,
                agent_id=canonical.agent_id, scope=scope, immutable=False, source_ref=CONSOLIDATOR_SOURCE,
                expected_version=canonical.version, expected_hash=canonical.content_hash,
                actor_user_id=user_id, trace_id=trace_id,
            )
            for item in duplicates:
                self.store.forget_scoped(
                    db, tenant_id=tenant_id, user_id=user_id, key=item.key,
                    agent_id=item.agent_id, scope=scope, actor_user_id=user_id, trace_id=trace_id,
                )
            merged.append(canonical.key)
            conflicts_removed += len(duplicates)

        new_cursor = {
            "last_created_at": datetime.now(timezone.utc).isoformat(),
            "last_id": str(rows[-1].id) if rows else None,
            "scanned": len(rows),
        }
        self.store.remember_scoped(
            db, tenant_id=tenant_id, user_id=user_id, key=_cursor_key(scope, agent_id),
            value=json.dumps(new_cursor, ensure_ascii=False), agent_id=agent_id, scope=scope,
            immutable=False, source_ref=CONSOLIDATOR_SOURCE,
            expected_version=cursor_row.version if cursor_row else None,
            expected_hash=cursor_row.content_hash if cursor_row else None,
            actor_user_id=user_id, trace_id=trace_id,
        )
        record_audit(
            db, tenant_id=tenant_id, actor_user_id=user_id, action="MEMORY_CONSOLIDATED",
            resource_type="MEMORY", resource_id=None, summary="dream-style memory consolidation",
            detail={"scope": scope, "scanned": len(rows), "conflicts_removed": conflicts_removed, "merged": len(merged)},
            trace_id=trace_id,
        )
        db.flush()
        return {"scanned": len(rows), "conflicts_removed": conflicts_removed, "merged": merged, "cursor": new_cursor}
