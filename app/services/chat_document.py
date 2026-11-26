"""会话文档输出：生成 ChangeSet 提案，用户 Keep 后才落地为可下载文件。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.kernel.audit.service import record_audit
from app.models import Conversation, File, Message
from app.models.file_version_change import FILE_CHANGE_CREATE, FILE_CHANGE_UPDATE
from app.services.document_export import build_document
from app.services.file_change_review import keep_file_change, propose_file_change


def create_chat_document(
    db: Session,
    *,
    tenant_id: UUID,
    user_id: UUID,
    conversation_id: UUID,
    object_store: object,
    max_bytes: int,
    max_extracted_chars: int,
    fmt: str,
    filename: str,
    content: str,
    task_id: UUID | None = None,
    purpose: str | None = None,
    risk: str | None = None,
    auto_keep: bool = False,
    trace_id: str | None = None,
) -> dict:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == tenant_id,
            Conversation.owner_user_id == user_id,
        )
    )
    if conversation is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "conversation not found", 404)
    try:
        _, name, media_type = build_document(fmt, filename, content)
    except ValueError as exc:
        raise ApiError("AI_REQUEST_INVALID", str(exc), 400) from exc

    previous = db.scalar(
        select(File)
        .where(
            File.tenant_id == tenant_id,
            File.owner_user_id == user_id,
            File.conversation_id == conversation.id,
            File.original_name == name,
            File.status != "DELETED",
        )
        .order_by(File.created_at.desc(), File.id.desc())
        .limit(1)
    )
    before_text = previous.extracted_text or "" if previous is not None else ""
    kind = FILE_CHANGE_UPDATE if previous is not None else FILE_CHANGE_CREATE
    after_text = (content or "")[:max_extracted_chars]

    assistant = Message(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        sender_type="ASSISTANT",
        source="console",
        content=f"文件变更提案：{name}（待确认）",
        content_parts=[],
        status="COMPLETED",
    )
    db.add(assistant)
    db.flush()

    change = propose_file_change(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation.id,
        name=name,
        media_type=media_type,
        before_text=before_text,
        after_text=after_text,
        previous_file_id=previous.id if previous is not None else None,
        message_id=assistant.id,
        task_id=task_id,
        fmt=fmt,
        kind=kind,
        purpose=purpose,
        risk=risk,
    )
    assistant.content_parts = [{
        "type": "file_change",
        "change_id": str(change.id),
        "file_name": name,
        "mime_type": media_type,
    }]
    record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        action="CHAT_DOCUMENT_EXPORTED",
        resource_type="FILE_CHANGE",
        resource_id=change.id,
        summary="document change proposal created in chat",
        detail={"format": fmt, "name": name, "conversation_id": str(conversation.id), "change_id": str(change.id)},
        trace_id=trace_id,
    )

    if auto_keep:
        keep_file_change(db, change, object_store=object_store, max_bytes=max_bytes)

    return {
        "message_id": str(assistant.id),
        "version_change_id": str(change.id),
        "file_id": str(change.current_file_id) if change.review_state == "KEPT" else None,
        "name": name,
        "format": fmt,
        "review_state": change.review_state,
        "download_url": f"/api/z-torque/files/{change.current_file_id}/raw" if change.review_state == "KEPT" else None,
    }
