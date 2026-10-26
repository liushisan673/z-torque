"""审计事件只读投影（内核，v1.3 §3.7 / B-12）。

插件读取审计事件流只经 `PluginSdk.read_audit(query)`；本模块实现 keyset 分页
（created_at, id 复合游标），每页一个短事务，避免长事务拖垮写入侧。
session_factory 由 SDK 注入（`PluginSdk.session`），内核不直连会话。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select

from app.kernel.audit.categories import audit_category
from app.models import AuditLog

DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 2000


@dataclass(frozen=True)
class AuditQuery:
    """审计事件查询条件（导出/归档的唯一读入口）。"""

    tenant_id: UUID
    actions: tuple[str, ...] | None = None
    action_prefix: str | None = None
    actor_user_id: UUID | None = None
    resource_type: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    page_size: int = DEFAULT_PAGE_SIZE

    def normalized_page_size(self) -> int:
        size = int(self.page_size or DEFAULT_PAGE_SIZE)
        return max(1, min(size, MAX_PAGE_SIZE))


@dataclass(frozen=True)
class AuditRecord:
    """对外稳定的审计事件投影（不暴露 ORM 对象，避免插件越权改状态）。"""

    id: str
    tenant_id: str
    actor_user_id: str | None
    action: str
    category: str
    resource_type: str
    resource_id: str | None
    summary: str
    detail: dict[str, Any] | None
    trace_id: str | None
    created_at: datetime


def to_record(item: AuditLog) -> AuditRecord:
    return AuditRecord(
        id=str(item.id),
        tenant_id=str(item.tenant_id),
        actor_user_id=str(item.actor_user_id) if item.actor_user_id else None,
        action=item.action,
        category=audit_category(item.action),
        resource_type=item.resource_type,
        resource_id=str(item.resource_id) if item.resource_id else None,
        summary=item.summary,
        detail=item.detail if isinstance(item.detail, dict) else None,
        trace_id=item.trace_id,
        created_at=item.created_at,
    )


def iter_audit_events(
    session_factory: Callable[[], AbstractContextManager[Any]],
    query: AuditQuery,
) -> Iterator[AuditRecord]:
    """按 (created_at, id) 升序 keyset 迭代；调用方按需 break（流式导出）。"""
    page_size = query.normalized_page_size()
    cursor_created: datetime | None = None
    cursor_id: UUID | None = None
    while True:
        with session_factory() as db:
            stmt = select(AuditLog).where(AuditLog.tenant_id == query.tenant_id)
            # 空元组语义 = 无匹配（生成恒假条件），None = 不过滤；两者不可混淆
            if query.actions is not None:
                stmt = stmt.where(AuditLog.action.in_(query.actions))
            if query.action_prefix:
                stmt = stmt.where(AuditLog.action.startswith(query.action_prefix))
            if query.actor_user_id is not None:
                stmt = stmt.where(AuditLog.actor_user_id == query.actor_user_id)
            if query.resource_type:
                stmt = stmt.where(AuditLog.resource_type == query.resource_type)
            if query.start_time is not None:
                stmt = stmt.where(AuditLog.created_at >= query.start_time)
            if query.end_time is not None:
                stmt = stmt.where(AuditLog.created_at <= query.end_time)
            if cursor_created is not None and cursor_id is not None:
                stmt = stmt.where(
                    or_(
                        AuditLog.created_at > cursor_created,
                        and_(AuditLog.created_at == cursor_created, AuditLog.id > cursor_id),
                    )
                )
            rows = db.scalars(stmt.order_by(AuditLog.created_at.asc(), AuditLog.id.asc()).limit(page_size)).all()
            # 会话关闭前完成投影，避免 ORM 实例脱离后属性失效（流式导出的紧耦合点）
            records = [to_record(row) for row in rows]
            if rows:
                cursor_created = rows[-1].created_at
                cursor_id = rows[-1].id
        if not records:
            return
        yield from records
        if len(records) < page_size:
            return
