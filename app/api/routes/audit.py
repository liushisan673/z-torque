from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response
from app.core.servertime import iso_utc
from app.db.session import get_db
from app.models import AuditLog, User
from app.schemas.audit import AuditEventResponse
from app.security.permissions import list_user_permissions

router = APIRouter(prefix="/audit", tags=["audit"])


def _infer_result(item: AuditLog) -> str | None:
    """从 detail 推断执行结果；无明确结果的配置类事件返回 None（前端显示 —）。"""
    detail = item.detail or {}
    status = detail.get("status")
    if isinstance(status, str):
        s = status.lower()
        if s in ("completed", "success", "succeeded", "ready"):
            return "success"
        if s in ("failed", "error"):
            return "failed"
        if s in ("denied", "rejected"):
            return "denied"
        return "pending"
    return None


def serialize_event(item: AuditLog, actor_name: str | None = None) -> dict:
    return AuditEventResponse(
        id=str(item.id),
        action=item.action,
        resource_type=item.resource_type,
        resource_id=str(item.resource_id) if item.resource_id else None,
        actor_user_id=str(item.actor_user_id) if item.actor_user_id else None,
        actor_username=actor_name,
        summary=item.summary,
        detail=item.detail,
        trace_id=item.trace_id,
        result=_infer_result(item),
        duration_ms=item.detail.get("duration_ms") if isinstance(item.detail, dict) else None,
        created_at=iso_utc(item.created_at),
    ).model_dump(mode="json")


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError("AI_PARAM_INVALID", f"invalid datetime: {value}", 400)


def _apply_scope(db: Session, user: User) -> bool:
    """audit.read_all 看全量；仅 audit.read 的用户强制只看自己相关事件。返回是否为全量范围。"""
    if "audit.read_all" in list_user_permissions(db, tenant_id=user.tenant_id, user_id=user.id):
        return True
    return False


@router.get("")
async def list_events(
    request: Request,
    action: str | None = Query(default=None),
    action_prefix: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    resource_id: UUID | None = Query(default=None),
    actor_user_id: UUID | None = Query(default=None),
    actor_username: str | None = Query(default=None),
    trace_id: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    start_time: str | None = Query(default=None),
    end_time: str | None = Query(default=None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # 数据范围按权限收缩：无 audit.read_all 的用户强制只看自己相关
    scope_all = _apply_scope(db, user)
    query = select(AuditLog).where(AuditLog.tenant_id == user.tenant_id)
    if not scope_all:
        query = query.where(AuditLog.actor_user_id == user.id)
    if action is not None:
        query = query.where(AuditLog.action == action)
    if action_prefix is not None:
        query = query.where(AuditLog.action.startswith(action_prefix))
    if resource_type is not None:
        query = query.where(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        query = query.where(AuditLog.resource_id == resource_id)
    if scope_all:
        if actor_user_id is not None:
            query = query.where(AuditLog.actor_user_id == actor_user_id)
        if actor_username:
            target = db.scalar(select(User.id).where(User.tenant_id == user.tenant_id, User.username == actor_username))
            query = query.where(AuditLog.actor_user_id == target if target else AuditLog.id.is_(None))
    if trace_id is not None:
        query = query.where(AuditLog.trace_id == trace_id)
    if keyword:
        query = query.where(AuditLog.summary.contains(keyword))
    if start_time is not None:
        query = query.where(AuditLog.created_at >= _parse_dt(start_time))
    if end_time is not None:
        query = query.where(AuditLog.created_at <= _parse_dt(end_time))
    items = db.scalars(query.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)).all()
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    # 批量联查执行用户名
    actor_ids = {x.actor_user_id for x in items if x.actor_user_id}
    name_map: dict[UUID, str] = {}
    if actor_ids:
        rows = db.scalars(select(User).where(User.id.in_(actor_ids))).all()
        name_map = {u.id: u.username for u in rows}
    return success_response(request, {
        "items": [serialize_event(x, name_map.get(x.actor_user_id)) for x in items],
        "offset": offset,
        "limit": limit,
        "total": total,
        "scope": "all" if scope_all else "self",
    })


@router.get("/{event_id}")
async def get_event(event_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = db.scalar(select(AuditLog).where(AuditLog.id == event_id, AuditLog.tenant_id == user.tenant_id))
    if item is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "audit event not found", 404)
    if not _apply_scope(db, user) and item.actor_user_id != user.id:
        raise ApiError("AI_FORBIDDEN", "no permission to view this audit event", 403)
    actor_name = None
    if item.actor_user_id:
        row = db.scalar(select(User.username).where(User.id == item.actor_user_id))
        actor_name = row
    return success_response(request, serialize_event(item, actor_name))


@router.get("/by-trace/{trace_id}")
async def list_by_trace(trace_id: str, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = select(AuditLog).where(AuditLog.tenant_id == user.tenant_id, AuditLog.trace_id == trace_id).order_by(AuditLog.created_at)
    if not _apply_scope(db, user):
        query = query.where(AuditLog.actor_user_id == user.id)
    items = db.scalars(query).all()
    actor_ids = {x.actor_user_id for x in items if x.actor_user_id}
    name_map: dict[UUID, str] = {}
    if actor_ids:
        rows = db.scalars(select(User).where(User.id.in_(actor_ids))).all()
        name_map = {u.id: u.username for u in rows}
    return success_response(request, {"items": [serialize_event(x, name_map.get(x.actor_user_id)) for x in items], "trace_id": trace_id})
