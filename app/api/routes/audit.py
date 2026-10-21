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


# 审计事件行为类别（action → category 显式映射，唯一数据源）：
# 审计页"类型"列标签与"事件类型"筛选共用，归属规则——
# 产生新已发布版本的动作归"版本发布"；SkillGateway 实际执行归"工具调用"；
# 纯模型推理归"模型调用"；其余资源管理操作归"配置变更"。未登记动作兜底"配置变更"。
CATEGORY_CONFIG = "config"
CATEGORY_MODEL_CALL = "model_call"
CATEGORY_TOOL_CALL = "tool_call"
CATEGORY_PUBLISH = "publish"
CATEGORY_FILE = "file"
CATEGORY_CONFIRMATION = "confirmation"
CATEGORY_TASK = "task"
CATEGORY_IAM = "iam"
CATEGORY_KERNEL = "kernel"

_AUDIT_CATEGORY_BY_ACTION: dict[str, str] = {
    # 模型调用
    "MODEL_CALL_COMPLETED": CATEGORY_MODEL_CALL,
    "MODEL_CALL_FAILED": CATEGORY_MODEL_CALL,
    # 工具调用
    "SKILL_EXECUTED": CATEGORY_TOOL_CALL,
    # 版本发布（产生新的不可变发布版本）
    "AGENT_VERSION_PUBLISHED": CATEGORY_PUBLISH,
    "TOOL_VERSION_PUBLISHED": CATEGORY_PUBLISH,
    "CONNECTOR_TOOL_REGISTERED": CATEGORY_PUBLISH,
    "CONNECTOR_TOOL_RECONFIGURED": CATEGORY_PUBLISH,
    "CONNECTOR_COMPOSITE_TOOL_REGISTERED": CATEGORY_PUBLISH,
    "CONNECTOR_COMPOSITE_TOOL_RECONFIGURED": CATEGORY_PUBLISH,
    "CONNECTOR_OPENAPI_IMPORTED": CATEGORY_PUBLISH,
    # 文件操作
    "FILE_UPLOADED": CATEGORY_FILE,
    "FILE_READ": CATEGORY_FILE,
    "FILE_DELETED": CATEGORY_FILE,
    # 确认审批
    "CONFIRMATION_REQUESTED": CATEGORY_CONFIRMATION,
    "CONFIRMATION_RESOLVED": CATEGORY_CONFIRMATION,
    # 任务运行
    "TASK_CANCELLED": CATEGORY_TASK,
    # 账号权限
    "USER_CREATED": CATEGORY_IAM,
    "USER_UPDATED": CATEGORY_IAM,
    "USER_DELETED": CATEGORY_IAM,
    "USER_PASSWORD_RESET": CATEGORY_IAM,
    "USER_UNLOCKED": CATEGORY_IAM,
    # 系统/内核
    "CAPABILITY_UNAVAILABLE": CATEGORY_KERNEL,
    "DEFAULT_PARENT_SET": CATEGORY_KERNEL,
    "DEFAULT_PARENT_BOOTSTRAPPED": CATEGORY_KERNEL,
    # 配置变更（资源与配置的管理操作：创建/修改/删除/归档/绑定）
    "AGENT_CREATED": CATEGORY_CONFIG,
    "AGENT_COPIED": CATEGORY_CONFIG,
    "AGENT_ARCHIVED": CATEGORY_CONFIG,
    "AGENT_DELETED": CATEGORY_CONFIG,
    "AGENT_VERSION_CREATED": CATEGORY_CONFIG,
    "AGENT_VERSION_ARCHIVED": CATEGORY_CONFIG,
    "AGENT_SKILL_ATTACHED": CATEGORY_CONFIG,
    "AGENT_SKILL_DETACHED": CATEGORY_CONFIG,
    "SKILL_CREATED": CATEGORY_CONFIG,
    "SKILL_UPDATED": CATEGORY_CONFIG,
    "SKILL_DELETED": CATEGORY_CONFIG,
    "SKILL_ARCHIVED": CATEGORY_CONFIG,
    "TOOL_CREATED": CATEGORY_CONFIG,
    "TOOL_UPDATED": CATEGORY_CONFIG,
    "TOOL_DELETED": CATEGORY_CONFIG,
    "TOOL_ARCHIVED": CATEGORY_CONFIG,
    "SKILL_TOOL_BINDING_UPGRADED": CATEGORY_CONFIG,
    "MD_SKILL_UPLOADED": CATEGORY_CONFIG,
    "MD_SKILL_DRAFT_CREATED": CATEGORY_CONFIG,
    "MD_SKILL_REGISTERED": CATEGORY_CONFIG,
    "MODEL_PROVIDER_CREATED": CATEGORY_CONFIG,
    "MODEL_PROVIDER_UPDATED": CATEGORY_CONFIG,
    "MODEL_PROVIDER_DISABLED": CATEGORY_CONFIG,
    "MODEL_PROVIDER_ENABLED": CATEGORY_CONFIG,
    "MODEL_ENDPOINT_CREATED": CATEGORY_CONFIG,
    "MODEL_ENDPOINT_UPDATED": CATEGORY_CONFIG,
    "MODEL_ENDPOINT_DISABLED": CATEGORY_CONFIG,
    "MODEL_ENDPOINT_ENABLED": CATEGORY_CONFIG,
    "MODEL_RESOURCE_CREATED": CATEGORY_CONFIG,
    "MODEL_RESOURCE_UPDATED": CATEGORY_CONFIG,
    "MODEL_RESOURCE_DISABLED": CATEGORY_CONFIG,
    "MODEL_RESOURCE_ENABLED": CATEGORY_CONFIG,
    "CONNECTOR_CREATED": CATEGORY_CONFIG,
    "CONNECTOR_UPDATED": CATEGORY_CONFIG,
    "CONNECTOR_DISABLED": CATEGORY_CONFIG,
    "CONNECTOR_ENABLED": CATEGORY_CONFIG,
    "CONNECTOR_ARCHIVED": CATEGORY_CONFIG,
    "CONNECTOR_DELETED": CATEGORY_CONFIG,
    "CONNECTOR_TESTED": CATEGORY_CONFIG,
    "CONNECTOR_DISCOVERED": CATEGORY_CONFIG,
}

# 类别 → 动作集合反查表：category 查询参数按 action IN 过滤
_ACTIONS_BY_CATEGORY: dict[str, tuple[str, ...]] = {}
for _action, _cat in _AUDIT_CATEGORY_BY_ACTION.items():
    _ACTIONS_BY_CATEGORY.setdefault(_cat, []).append(_action)


def audit_category(item: AuditLog) -> str:
    return _AUDIT_CATEGORY_BY_ACTION.get(item.action, CATEGORY_CONFIG)


def serialize_event(item: AuditLog, actor_name: str | None = None) -> dict:
    return AuditEventResponse(
        id=str(item.id),
        action=item.action,
        category=audit_category(item),
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


@router.get("", summary="查询审计事件")
async def list_events(
    request: Request,
    action: str | None = Query(default=None),
    action_prefix: str | None = Query(default=None),
    category: str | None = Query(default=None),
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
    if category is not None:
        # 行为类别过滤：按类别映射的动作集合 IN 查询（未知类别返回空集）
        query = query.where(AuditLog.action.in_(_ACTIONS_BY_CATEGORY.get(category, [])))
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


@router.get("/{event_id}", summary="获取审计详情")
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


@router.get("/by-trace/{trace_id}", summary="查询Trace链路")
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
