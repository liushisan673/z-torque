"""文件变更提案（ChangeSet）服务：先提案、后落地。

所有新建/修改/回退都先写 `zr_file_version_change`，只有用户 Keep 后才写对象存储、
创建/切换 `File` 附件；Cancel 不触碰磁盘；回退基于历史快照生成反向提案。
"""

from __future__ import annotations

import hashlib
from io import BytesIO
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.ids import new_uuid7, utc_now
from app.kernel.audit.service import record_audit
from app.models import File, FileVersionChange, Message
from app.models.file_version_change import (
    FILE_CHANGE_ROLLBACK,
    FILE_CHANGE_UPDATE,
    FILE_REVIEW_CANCELLED,
    FILE_REVIEW_KEPT,
)
from app.models.message import message_file
from app.services.document_export import build_document
from app.services.text_diff import line_changes


def propose_file_change(
    db: Session,
    *,
    tenant_id: UUID,
    user_id: UUID,
    conversation_id: UUID,
    name: str,
    media_type: str,
    after_text: str,
    before_text: str = "",
    previous_file_id: UUID | None = None,
    current_file_id: UUID | None = None,
    message_id: UUID | None = None,
    task_id: UUID | None = None,
    fmt: str | None = None,
    kind: str = FILE_CHANGE_UPDATE,
    purpose: str | None = None,
    risk: str | None = None,
) -> FileVersionChange:
    additions, deletions = line_changes(before_text, after_text)
    change = FileVersionChange(
        tenant_id=tenant_id,
        owner_user_id=user_id,
        conversation_id=conversation_id,
        task_id=task_id,
        name=name,
        media_type=media_type,
        previous_file_id=previous_file_id,
        current_file_id=current_file_id or new_uuid7(),
        message_id=message_id,
        kind=kind,
        fmt=fmt,
        purpose=purpose,
        risk=risk,
        before_text=before_text,
        after_text=after_text,
        content_hash=hashlib.sha256(after_text.encode("utf-8")).hexdigest(),
        additions=additions,
        deletions=deletions,
    )
    db.add(change)
    db.flush()
    record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        action="FILE_CHANGE_PROPOSED",
        resource_type="FILE_CHANGE",
        resource_id=change.id,
        summary="file change proposal created",
        detail={
            "change_id": str(change.id),
            "name": name,
            "kind": kind,
            "conversation_id": str(conversation_id),
        },
        trace_id=None,
    )
    return change


def _update_message(db: Session, change: FileVersionChange, *, content: str, file_id: UUID | None = None) -> None:
    if change.message_id is None:
        return
    message = db.get(Message, change.message_id)
    if message is None:
        return
    message.content = content
    if file_id is None:
        message.content_parts = []
        return
    file = db.get(File, file_id)
    if file is None:
        message.content_parts = []
        return
    message.content_parts = [{
        "type": "file",
        "file_id": str(file.id),
        "file_name": file.original_name,
        "mime_type": file.media_type,
    }]
    db.execute(message_file.insert().values(
        message_id=message.id,
        file_id=file.id,
        tenant_id=change.tenant_id,
    ))


def keep_file_change(db: Session, change: FileVersionChange, *, object_store: object, max_bytes: int) -> None:
    if change.review_state != "PENDING":
        raise ApiError("AI_REQUEST_INVALID", "only pending proposals can be kept", 409)

    if change.kind == FILE_CHANGE_ROLLBACK:
        current = db.get(File, change.current_file_id)
        if current is not None and current.tenant_id == change.tenant_id:
            current.status = "DELETED"
        restored = db.get(File, change.previous_file_id) if change.previous_file_id else None
        if restored is not None and restored.tenant_id == change.tenant_id:
            restored.status = "PARSED"
        change.review_state = FILE_REVIEW_KEPT
        change.reviewed_at = utc_now()
        _update_message(db, change, content=f"已回退文件：{change.name}", file_id=change.previous_file_id)
        record_audit(
            db,
            tenant_id=change.tenant_id,
            actor_user_id=change.owner_user_id,
            action="FILE_CHANGE_KEPT",
            resource_type="FILE_CHANGE",
            resource_id=change.id,
            summary="rollback proposal kept",
            detail={"change_id": str(change.id), "name": change.name, "kind": change.kind},
            trace_id=None,
        )
        db.flush()
        return

    data, name, media_type = build_document(change.fmt or "md", change.name, change.after_text)
    stored = object_store.put(str(change.current_file_id), BytesIO(data), max_bytes=max_bytes)
    item = File(
        id=change.current_file_id,
        tenant_id=change.tenant_id,
        owner_user_id=change.owner_user_id,
        conversation_id=change.conversation_id,
        original_name=name,
        media_type=media_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        storage_key=str(change.current_file_id),
        status="PARSED",
        extracted_text=change.after_text,
    )
    db.add(item)
    change.review_state = FILE_REVIEW_KEPT
    change.reviewed_at = utc_now()
    db.flush()
    _update_message(db, change, content=f"已生成文件：{name}", file_id=change.current_file_id)
    record_audit(
        db,
        tenant_id=change.tenant_id,
        actor_user_id=change.owner_user_id,
        action="FILE_CHANGE_KEPT",
        resource_type="FILE_CHANGE",
        resource_id=change.id,
        summary="file change proposal kept",
        detail={"change_id": str(change.id), "name": name, "kind": change.kind, "file_id": str(change.current_file_id)},
        trace_id=None,
    )
    db.flush()


def cancel_file_change(db: Session, change: FileVersionChange) -> None:
    if change.review_state != "PENDING":
        raise ApiError("AI_REQUEST_INVALID", "only pending proposals can be cancelled", 409)
    change.review_state = FILE_REVIEW_CANCELLED
    change.reviewed_at = utc_now()
    _update_message(db, change, content=f"已放弃文件变更：{change.name}")
    record_audit(
        db,
        tenant_id=change.tenant_id,
        actor_user_id=change.owner_user_id,
        action="FILE_CHANGE_CANCELLED",
        resource_type="FILE_CHANGE",
        resource_id=change.id,
        summary="file change proposal cancelled",
        detail={"change_id": str(change.id), "name": change.name, "kind": change.kind},
        trace_id=None,
    )
    db.flush()


def propose_rollback(db: Session, change: FileVersionChange) -> FileVersionChange:
    if change.review_state != FILE_REVIEW_KEPT or change.kind == FILE_CHANGE_ROLLBACK:
        raise ApiError("AI_REQUEST_INVALID", "only an applied change can be rolled back", 409)
    existing = db.scalar(
        select(FileVersionChange).where(
            FileVersionChange.tenant_id == change.tenant_id,
            FileVersionChange.owner_user_id == change.owner_user_id,
            FileVersionChange.current_file_id == change.current_file_id,
            FileVersionChange.kind == FILE_CHANGE_ROLLBACK,
            FileVersionChange.review_state == "PENDING",
        )
    )
    if existing is not None:
        return existing
    return propose_file_change(
        db,
        tenant_id=change.tenant_id,
        user_id=change.owner_user_id,
        conversation_id=change.conversation_id,
        name=change.name,
        media_type=change.media_type,
        after_text=change.before_text,
        before_text=change.after_text,
        previous_file_id=change.previous_file_id,
        current_file_id=change.current_file_id,
        task_id=change.task_id,
        fmt=change.fmt,
        kind=FILE_CHANGE_ROLLBACK,
        purpose="回退到历史版本",
        risk="放弃当前版本并恢复上一版快照",
    )
