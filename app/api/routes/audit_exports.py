"""审计导出 / 归档的受控 API（v1.3 §5.5）。

三组能力：
- POST /audit-exports                             创建导出任务（需 audit.export）；
- GET  /audit-exports/{id}                        任务状态（需 audit.read）；
- GET  /audit-exports/{id}/shards/{seq}/download  取分片（需 audit.export）；
- POST /audit-archive-runs                        触发归档（需 audit.archive）；
- GET  /audit-archive-runs                        批次列表（需 audit.read）。

两条纪律：
1. **能力先于权限**：插件未装配 → 503（不泄露"你没权限"与"没这功能"的差异）；
2. **范围不静默降级**：无 audit.read_all 的调用者显式请求他人范围 → 403
   `AI_AUDIT_EXPORT_DENIED`（否则会误以为导出了全量），拒绝路径同时写审计。

本模块不 import 插件包：服务键盘来自 `app.state`，键名是本文件内的常量（阶段00 §3.8）。
"""

from __future__ import annotations

from functools import partial
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response, trace_id_from
from app.core.servertime import iso_utc
from app.db.session import get_db
from app.kernel.audit.service import record_audit_detached
from app.models import User
from app.schemas.audit_export import AuditArchiveTriggerRequest, AuditExportCreateRequest
from app.security.permissions import list_user_permissions

# app.state 上的服务键（与插件 sdk.publish 的键一致；此处用字面量避免反向依赖插件包）
SERVICE_AUDIT_EXPORT = "audit_export_service"
SERVICE_AUDIT_ARCHIVE = "audit_archive_service"

router = APIRouter(prefix="/audit-exports", tags=["audit"])
archive_router = APIRouter(prefix="/audit-archive-runs", tags=["audit"])


def _has_permission(db: Session, user: User, permission: str) -> bool:
    return permission in list_user_permissions(db, tenant_id=user.tenant_id, user_id=user.id)


def _require_permission(db: Session, user: User, permission: str, *, trace_id: str | None = None) -> None:
    if _has_permission(db, user, permission):
        return
    record_audit_detached(
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="AUDIT_EXPORT_DENIED",
        resource_type="AUDIT_EXPORT_JOB",
        resource_id=None,
        summary=f"audit export denied: missing {permission}",
        detail={"permission": permission},
        trace_id=trace_id,
    )
    raise ApiError("AI_FORBIDDEN", f"missing permission: {permission}", 403)


def _export_service(request: Request):
    service = getattr(request.app.state, SERVICE_AUDIT_EXPORT, None)
    if service is None:
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "audit_export_archive plugin is not assembled", 503)
    return service


def _archive_service(request: Request):
    service = getattr(request.app.state, SERVICE_AUDIT_ARCHIVE, None)
    if service is None:
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "audit_export_archive plugin is not assembled", 503)
    return service


@router.post("", summary="创建审计导出任务")
async def create_export(
    payload: AuditExportCreateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = _export_service(request)  # 503 优先于 403
    _require_permission(db, user, "audit.export", trace_id=trace_id_from(request))
    scope_all = _has_permission(db, user, "audit.read_all")
    if payload.actor_user_id is not None and payload.actor_user_id != user.id and not scope_all:
        record_audit_detached(
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            action="AUDIT_EXPORT_DENIED",
            resource_type="AUDIT_EXPORT_JOB",
            resource_id=None,
            summary="audit export denied: scope exceeds caller visibility",
            detail={"requested_actor": str(payload.actor_user_id)},
            trace_id=trace_id_from(request),
        )
        raise ApiError("AI_AUDIT_EXPORT_DENIED", "export scope exceeds caller visibility", 403)
    if payload.start_time and payload.end_time and payload.start_time > payload.end_time:
        raise ApiError("AI_PARAM_INVALID", "start_time must be earlier than end_time", 400)
    actions = payload.resolved_actions()
    target_actor = payload.actor_user_id if scope_all else user.id
    # 服务层是同步 DB + 同步对象存储；直接 await 会把整个事件循环卡在这条请求上
    job = await run_in_threadpool(
        partial(
            service.create_job,
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            filters={
                "actions": list(actions) if actions is not None else None,
                "actor_user_id": str(target_actor) if target_actor is not None else None,
                "resource_type": payload.resource_type,
                "start_time": iso_utc(payload.start_time),
                "end_time": iso_utc(payload.end_time),
            },
            export_format=payload.format,
            trace_id=trace_id_from(request),
        )
    )
    return success_response(request, job)


@router.get("", summary="导出任务列表")
async def list_exports(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = _export_service(request)
    _require_permission(db, user, "audit.read", trace_id=trace_id_from(request))
    items = await run_in_threadpool(partial(service.list_jobs, tenant_id=user.tenant_id, limit=limit))
    return success_response(request, {"items": items})


@router.get("/{job_id}", summary="导出任务状态")
async def get_export(
    job_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = _export_service(request)
    _require_permission(db, user, "audit.read", trace_id=trace_id_from(request))
    job = await run_in_threadpool(partial(service.get_job, tenant_id=user.tenant_id, job_id=job_id))
    return success_response(request, job)


@router.get("/{job_id}/shards/{seq}/download", summary="下载导出分片")
async def download_shard(
    job_id: UUID,
    seq: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = _export_service(request)
    _require_permission(db, user, "audit.export", trace_id=trace_id_from(request))
    path, filename = await run_in_threadpool(
        partial(service.open_shard, tenant_id=user.tenant_id, job_id=job_id, seq=seq, actor_user_id=user.id)
    )
    return FileResponse(path, media_type="application/octet-stream", filename=filename)


@archive_router.post("", summary="触发审计归档")
async def trigger_archive(
    payload: AuditArchiveTriggerRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = _archive_service(request)
    _require_permission(db, user, "audit.archive", trace_id=trace_id_from(request))
    # 归档是"读审计 + 写对象"的同步长流程：必须离开事件循环，否则一次归档阻塞全部请求
    run = await run_in_threadpool(
        partial(
            service.trigger,
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            lookback_hours=payload.lookback_hours,
            trigger_source="MANUAL",
        )
    )
    return success_response(request, run)


@archive_router.get("", summary="归档批次列表")
async def list_archive_runs(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = _archive_service(request)
    _require_permission(db, user, "audit.read", trace_id=trace_id_from(request))
    items = await run_in_threadpool(partial(service.list_runs, tenant_id=user.tenant_id, limit=limit))
    return success_response(request, {"items": items})
