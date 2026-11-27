"""工作区事务式补丁入口与会话文件变更审查（Phase 0 §2.3）。

真实写入入口只经 SkillGateway：driver 不直接暴露给 API/服务层，scope 与审计都在网关收口。
审查接口只读取带 TTL 的补丁正文，审计仍保持 metadata-only。
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.config import get_settings
from app.core.errors import ApiError
from app.core.ids import utc_now
from app.core.responses import success_response
from app.db.session import get_db
from app.kernel.audit.service import record_audit
from app.kernel.security_context import SecurityContext
from app.models import Conversation, File, FileVersionChange, PatchArtifact, Task, User
from app.models.file_version_change import (
    FILE_CHANGE_ROLLBACK,
    FILE_REVIEW_KEPT,
    FILE_REVIEW_PENDING,
)
from app.models.patch_artifact import PATCH_REVIEW_KEPT, PATCH_REVIEW_PENDING, PATCH_REVIEW_UNDONE
from app.schemas.patch import PatchApplyRequest
from app.services.file_change_review import (
    cancel_file_change as cancel_file_change_service,
)
from app.services.file_change_review import (
    keep_file_change as keep_file_change_service,
)
from app.services.file_change_review import (
    propose_rollback as propose_rollback_service,
)
from app.services.document_export import build_document
from app.services.text_diff import line_changes

router = APIRouter(prefix="/workspace", tags=["workspace"])


@router.post("/patch", summary="事务式写入补丁（经 SkillGateway 唯一入口）")
async def apply_patch(payload: PatchApplyRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    gateway = getattr(request.app.state, "skill_gateway", None)
    if gateway is None:
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "skill gateway is not available", 503)
    ctx = SecurityContext(tenant_id=user.tenant_id, user_id=user.id, trace_id=getattr(request.state, "trace_id", None))
    result = await gateway.apply_patch(
        db, ctx,
        patches=[item.model_dump() for item in payload.patches],
        base_dir=payload.base_dir,
        task_id=payload.task_id,
    )
    db.commit()
    return success_response(request, result)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _display_state(artifact: PatchArtifact, now: datetime) -> str:
    if artifact.review_state == PATCH_REVIEW_PENDING and _as_utc(artifact.expires_at) <= now:
        return "EXPIRED"
    return artifact.review_state


def _latest_artifact_ids(db: Session, user: User) -> dict[str, UUID]:
    rows = db.scalars(
        select(PatchArtifact)
        .join(Task, PatchArtifact.task_id == Task.id)
        .where(
            PatchArtifact.tenant_id == user.tenant_id,
            Task.tenant_id == user.tenant_id,
            Task.owner_user_id == user.id,
        )
        .order_by(PatchArtifact.created_at.asc(), PatchArtifact.id.asc())
    ).all()
    latest: dict[str, UUID] = {}
    for row in rows:
        if row.review_state == PATCH_REVIEW_UNDONE:
            continue
        latest[row.path] = row.id
    return latest


def _artifact_payload(
    artifact: PatchArtifact,
    latest_ids: dict[str, UUID],
    *,
    include_text: bool = False,
) -> dict:
    additions, deletions = line_changes(artifact.before_text, artifact.after_text)
    now = utc_now()
    state = _display_state(artifact, now)
    payload = {
        "id": str(artifact.id),
        "source": "patch",
        "task_id": str(artifact.task_id) if artifact.task_id else None,
        "path": artifact.path,
        "content_hash": artifact.content_hash,
        "review_state": state,
        "created_at": artifact.created_at.isoformat(),
        "expires_at": artifact.expires_at.isoformat(),
        "additions": additions,
        "deletions": deletions,
        "can_undo": bool(
            artifact.base_dir
            and state in (PATCH_REVIEW_PENDING, PATCH_REVIEW_KEPT)
            and latest_ids.get(artifact.path) == artifact.id
        ),
    }
    if include_text:
        payload.update({
            "before_text": artifact.before_text,
            "after_text": artifact.after_text,
            "base_dir": artifact.base_dir,
        })
    return payload


def _owned_artifact(db: Session, user: User, artifact_id: UUID) -> PatchArtifact:
    artifact = db.scalar(
        select(PatchArtifact)
        .join(Task, PatchArtifact.task_id == Task.id)
        .where(
            PatchArtifact.id == artifact_id,
            PatchArtifact.tenant_id == user.tenant_id,
            Task.tenant_id == user.tenant_id,
            Task.owner_user_id == user.id,
        )
    )
    if artifact is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "patch artifact not found", 404)
    return artifact


@router.get("/patch-artifacts", summary="按会话列出文件变更")
def list_patch_artifacts(
    conversation_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == user.tenant_id,
            Conversation.owner_user_id == user.id,
        )
    )
    if conversation is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "conversation not found", 404)
    artifacts = db.scalars(
        select(PatchArtifact)
        .join(Task, PatchArtifact.task_id == Task.id)
        .where(
            PatchArtifact.tenant_id == user.tenant_id,
            Task.tenant_id == user.tenant_id,
            Task.owner_user_id == user.id,
            Task.conversation_id == conversation_id,
        )
        .order_by(PatchArtifact.created_at.desc(), PatchArtifact.id.desc())
    ).all()
    latest_ids = _latest_artifact_ids(db, user)
    return success_response(request, [_artifact_payload(item, latest_ids) for item in artifacts])


@router.get("/patch-artifacts/{artifact_id}", summary="获取单条文件变更的正文")
def get_patch_artifact(
    artifact_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    artifact = _owned_artifact(db, user, artifact_id)
    return success_response(request, _artifact_payload(artifact, _latest_artifact_ids(db, user), include_text=True))


@router.post("/patch-artifacts/{artifact_id}/keep", summary="保留文件变更")
def keep_patch_artifact(
    artifact_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    artifact = _owned_artifact(db, user, artifact_id)
    if artifact.review_state == PATCH_REVIEW_UNDONE:
        raise ApiError("AI_REQUEST_INVALID", "undone patch cannot be kept", 409)
    if artifact.review_state != PATCH_REVIEW_KEPT:
        artifact.review_state = PATCH_REVIEW_KEPT
        artifact.reviewed_at = utc_now()
        record_audit(
            db,
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            action="PATCH_KEEP",
            resource_type="PATCH_ARTIFACT",
            resource_id=artifact.id,
            summary="patch kept by user",
            detail={"artifact_id": str(artifact.id), "path": artifact.path, "hash": artifact.content_hash},
            trace_id=getattr(request.state, "trace_id", None),
        )
        db.commit()
    return success_response(request, _artifact_payload(artifact, _latest_artifact_ids(db, user)))


@router.post("/patch-artifacts/{artifact_id}/undo", summary="撤销文件变更（真实写回）")
async def undo_patch_artifact(
    artifact_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    artifact = _owned_artifact(db, user, artifact_id)
    now = utc_now()
    if _as_utc(artifact.expires_at) <= now:
        raise ApiError("AI_PATCH_EXPIRED", "patch artifact expired", 409)
    if artifact.review_state == PATCH_REVIEW_UNDONE:
        raise ApiError("AI_REQUEST_INVALID", "patch already undone", 409)
    if not artifact.base_dir:
        raise ApiError("AI_REQUEST_INVALID", "patch has no workspace path", 409)
    if _latest_artifact_ids(db, user).get(artifact.path) != artifact.id:
        raise ApiError("AI_PATCH_OUTDATED", "a newer change exists for this file", 409)

    gateway = getattr(request.app.state, "skill_gateway", None)
    if gateway is None:
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "skill gateway is not available", 503)
    ctx = SecurityContext(tenant_id=user.tenant_id, user_id=user.id, trace_id=getattr(request.state, "trace_id", None))
    await gateway.apply_patch(
        db,
        ctx,
        patches=[{
            "path": artifact.path,
            "old_text": artifact.after_text,
            "new_text": artifact.before_text,
            "expected_hash": artifact.content_hash,
        }],
        base_dir=artifact.base_dir,
        task_id=artifact.task_id,
        record_artifact=False,
    )
    artifact.review_state = PATCH_REVIEW_UNDONE
    artifact.reviewed_at = utc_now()
    record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="PATCH_UNDO",
        resource_type="PATCH_ARTIFACT",
        resource_id=artifact.id,
        summary="patch undone by user",
        detail={"artifact_id": str(artifact.id), "path": artifact.path, "hash": artifact.content_hash},
        trace_id=getattr(request.state, "trace_id", None),
    )
    db.commit()
    return success_response(request, _artifact_payload(artifact, _latest_artifact_ids(db, user)))


def _latest_file_ids(db: Session, user: User, conversation_id: UUID) -> dict[str, UUID]:
    rows = db.scalars(
        select(File)
        .where(
            File.tenant_id == user.tenant_id,
            File.owner_user_id == user.id,
            File.conversation_id == conversation_id,
            File.status != "DELETED",
        )
        .order_by(File.created_at.asc(), File.id.asc())
    ).all()
    latest: dict[str, UUID] = {}
    for row in rows:
        latest[row.original_name] = row.id
    return latest


def _pending_rollback_targets(db: Session, user: User, conversation_id: UUID) -> set[UUID]:
    rows = db.scalars(
        select(FileVersionChange.current_file_id).where(
            FileVersionChange.tenant_id == user.tenant_id,
            FileVersionChange.owner_user_id == user.id,
            FileVersionChange.conversation_id == conversation_id,
            FileVersionChange.kind == FILE_CHANGE_ROLLBACK,
            FileVersionChange.review_state == FILE_REVIEW_PENDING,
        )
    ).all()
    return set(rows)


def _file_change_payload(
    change: FileVersionChange,
    latest_ids: dict[str, UUID],
    rollback_targets: set[UUID],
    *,
    include_text: bool = False,
) -> dict:
    state = change.review_state
    kept_file_id = str(change.current_file_id) if state == FILE_REVIEW_KEPT and change.current_file_id else None
    download_url = None
    if kept_file_id:
        download_url = f"/api/z-torque/files/{kept_file_id}/raw"
    elif state == FILE_REVIEW_PENDING:
        # 待评审文档尚未落盘：下载端点按提案正文动态生成，不改变审查状态
        download_url = f"/api/z-torque/workspace/file-changes/{change.id}/download"
    payload = {
        "id": str(change.id),
        "source": "file",
        "task_id": str(change.task_id) if change.task_id else None,
        "path": change.name,
        "name": change.name,
        "media_type": change.media_type,
        "file_id": kept_file_id,
        "download_url": download_url,
        "content_hash": change.content_hash,
        "review_state": state,
        "kind": change.kind,
        "fmt": change.fmt,
        "purpose": change.purpose,
        "risk": change.risk,
        "created_at": change.created_at.isoformat(),
        "expires_at": None,
        "additions": change.additions,
        "deletions": change.deletions,
        "can_keep": state == FILE_REVIEW_PENDING,
        "can_cancel": state == FILE_REVIEW_PENDING,
        "can_rollback": bool(
            state == FILE_REVIEW_KEPT
            and change.kind != FILE_CHANGE_ROLLBACK
            and change.current_file_id not in rollback_targets
            and latest_ids.get(change.name) == change.current_file_id
        ),
        "can_undo": bool(state == FILE_REVIEW_KEPT and change.current_file_id not in rollback_targets),
    }
    if include_text:
        payload.update({
            "before_text": change.before_text,
            "after_text": change.after_text,
            "base_dir": None,
        })
    return payload


def _owned_file_change(db: Session, user: User, change_id: UUID) -> FileVersionChange:
    change = db.scalar(
        select(FileVersionChange).where(
            FileVersionChange.id == change_id,
            FileVersionChange.tenant_id == user.tenant_id,
            FileVersionChange.owner_user_id == user.id,
        )
    )
    if change is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "file version change not found", 404)
    return change


def _file_change_response(db: Session, user: User, change: FileVersionChange, *, include_text: bool = False) -> dict:
    latest_ids = _latest_file_ids(db, user, change.conversation_id)
    rollback_targets = _pending_rollback_targets(db, user, change.conversation_id)
    return _file_change_payload(change, latest_ids, rollback_targets, include_text=include_text)


@router.get("/file-changes", summary="按会话列出同名文件版本变更")
def list_file_changes(
    conversation_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == user.tenant_id,
            Conversation.owner_user_id == user.id,
        )
    )
    if conversation is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "conversation not found", 404)
    changes = db.scalars(
        select(FileVersionChange)
        .where(
            FileVersionChange.tenant_id == user.tenant_id,
            FileVersionChange.owner_user_id == user.id,
            FileVersionChange.conversation_id == conversation_id,
        )
        .order_by(FileVersionChange.created_at.desc(), FileVersionChange.id.desc())
    ).all()
    latest_ids = _latest_file_ids(db, user, conversation_id)
    rollback_targets = _pending_rollback_targets(db, user, conversation_id)
    return success_response(request, [
        _file_change_payload(item, latest_ids, rollback_targets) for item in changes
    ])


@router.get("/file-changes/{change_id}", summary="获取文件版本变更正文")
def get_file_change(
    change_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    change = _owned_file_change(db, user, change_id)
    latest_ids = _latest_file_ids(db, user, change.conversation_id)
    rollback_targets = _pending_rollback_targets(db, user, change.conversation_id)
    return success_response(request, _file_change_payload(change, latest_ids, rollback_targets, include_text=True))


@router.get("/file-changes/{change_id}/download", summary="下载文件变更（已采纳下载原文件，待评审下载草稿）")
def download_file_change(
    change_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """已采纳：返回落盘原文件（docx/pdf 等二进制）；待评审：按提案正文即时生成草稿字节。

    草稿下载不写对象存储、不改变 review_state，语义仍是「Keep 后才真正保存」。
    """
    change = _owned_file_change(db, user, change_id)
    if change.review_state == FILE_REVIEW_KEPT and change.current_file_id:
        item = db.get(File, change.current_file_id)
        registry = getattr(request.app.state, "port_registry", None)
        store = registry.get("object.store") if registry is not None else None
        if item is not None and item.status != "DELETED" and store is not None:
            target = store.local_path(item.storage_key)
            if target.is_file():
                record_audit(
                    db,
                    tenant_id=user.tenant_id,
                    actor_user_id=user.id,
                    action="FILE_READ",
                    resource_type="FILE",
                    resource_id=item.id,
                    summary="kept file change downloaded",
                    detail={"change_id": str(change.id), "name": item.original_name},
                    trace_id=None,
                )
                db.commit()
                return FileResponse(
                    target,
                    media_type=item.media_type or "application/octet-stream",
                    filename=item.original_name,
                    content_disposition_type="attachment",
                )
    try:
        data, name, media_type = build_document(change.fmt or "md", change.name, change.after_text or "")
    except ValueError as exc:
        raise ApiError("AI_FILE_TYPE_UNSUPPORTED", "unsupported document format", 415) from exc
    record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="FILE_READ",
        resource_type="FILE_CHANGE",
        resource_id=change.id,
        summary="file change draft downloaded",
        detail={"change_id": str(change.id), "name": name, "review_state": change.review_state},
        trace_id=None,
    )
    db.commit()
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


@router.post("/file-changes/{change_id}/keep", summary="采纳文件变更提案（Keep 后才落地）")
def keep_file_change_route(
    change_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    change = _owned_file_change(db, user, change_id)
    if change.review_state == FILE_REVIEW_KEPT:
        return success_response(request, _file_change_response(db, user, change))
    if change.review_state != FILE_REVIEW_PENDING:
        raise ApiError("AI_REQUEST_INVALID", "only pending proposals can be kept", 409)
    object_store = getattr(request.app.state, "port_registry", None)
    object_store = object_store.get("object.store") if object_store is not None else None
    if object_store is None:
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "object.store is not available", 503)
    settings = get_settings()
    keep_file_change_service(
        db,
        change,
        object_store=object_store,
        max_bytes=settings.max_upload_size_mb * 1024 * 1024,
    )
    db.commit()
    return success_response(request, _file_change_response(db, user, change))


@router.post("/file-changes/{change_id}/cancel", summary="放弃文件变更提案（不触碰磁盘）")
def cancel_file_change_route(
    change_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    change = _owned_file_change(db, user, change_id)
    cancel_file_change_service(db, change)
    db.commit()
    return success_response(request, _file_change_response(db, user, change))


@router.post("/file-changes/{change_id}/undo", summary="生成回退提案（仍需 Keep 才实际回退）")
def undo_file_change(
    change_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    change = _owned_file_change(db, user, change_id)
    proposed = propose_rollback_service(db, change)
    db.commit()
    return success_response(request, _file_change_response(db, user, proposed))
