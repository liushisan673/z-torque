import hashlib
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi import File as UploadFileParam
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.config import get_settings
from app.core.errors import ApiError
from app.core.ids import new_uuid7
from app.core.responses import success_response
from app.core.servertime import iso_utc
from app.db.session import get_db
from app.models import Conversation, File, User
from app.models.message import message_file
from app.kernel.audit.service import record_audit
from app.services.file_parser import SUPPORTED, parse_file

router = APIRouter(prefix="/files", tags=["files"])


def owned_file(db: Session, user: User, file_id: UUID) -> File:
    item = db.scalar(select(File).where(File.id == file_id, File.tenant_id == user.tenant_id, File.owner_user_id == user.id))
    if item is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "file not found", 404)
    return item


def _signature_matches(path: Path, suffix: str) -> bool:
    header = path.read_bytes()[:8]
    return {".pdf": header.startswith(b"%PDF"), ".xlsx": header.startswith(b"PK"), ".txt": True, ".md": True, ".csv": True}.get(suffix, False)


def metadata(item: File) -> dict:
    return {"id": str(item.id), "name": item.original_name, "media_type": item.media_type, "size_bytes": item.size_bytes, "status": item.status, "created_at": iso_utc(item.created_at)}


@router.post("", summary="上传文件")
async def upload(request: Request, upload: UploadFile = UploadFileParam(...), conversation_id: UUID | None = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    settings = get_settings()
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in SUPPORTED:
        raise ApiError("AI_FILE_TYPE_UNSUPPORTED", "file type is not supported", 415)
    conversation = None
    if conversation_id:
        conversation = db.scalar(select(Conversation).where(Conversation.id == conversation_id, Conversation.tenant_id == user.tenant_id, Conversation.owner_user_id == user.id))
        if conversation is None:
            raise ApiError("AI_RESOURCE_NOT_FOUND", "conversation not found", 404)
    file_id = new_uuid7()
    root = settings.file_storage_dir.resolve()
    target = (root / str(file_id)).resolve()
    if root not in target.parents:
        raise ApiError("AI_INTERNAL_ERROR", "invalid file storage path", 500)
    digest = hashlib.sha256()
    size = 0
    with target.open("wb") as handle:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > settings.max_upload_size_mb * 1024 * 1024:
                target.unlink(missing_ok=True)
                raise ApiError("AI_FILE_TOO_LARGE", "file exceeds configured limit", 413)
            digest.update(chunk)
            handle.write(chunk)
    if not _signature_matches(target, suffix):
        target.unlink(missing_ok=True)
        raise ApiError("AI_FILE_TYPE_UNSUPPORTED", "file content does not match its type", 415)
    item = File(id=file_id, tenant_id=user.tenant_id, owner_user_id=user.id, conversation_id=conversation_id, original_name=Path(upload.filename or "file").name[:255], media_type=upload.content_type or "application/octet-stream", size_bytes=size, sha256=digest.hexdigest(), storage_key=str(file_id), status="UPLOADED")
    try:
        item.extracted_text = parse_file(target, suffix, settings.file_max_extracted_text)
        item.status = "PARSED"
    except (OSError, ValueError) as exc:
        item.status = "PARSE_FAILED"
        item.parse_error = str(exc)[:500]
    db.add(item)
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="FILE_UPLOADED", resource_type="FILE", resource_id=item.id, summary="file uploaded", trace_id=request.state.trace_id)
    db.commit()
    db.refresh(item)
    return success_response(request, metadata(item))


@router.get("/{file_id}", summary="获取文件详情")
async def detail(file_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return success_response(request, metadata(owned_file(db, user, file_id)))


@router.get("/{file_id}/status", summary="获取文件状态")
async def status(file_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = owned_file(db, user, file_id)
    return success_response(request, {"id": str(item.id), "status": item.status, "error": item.parse_error})


@router.get("/{file_id}/content", summary="获取文件内容")
async def content(file_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = owned_file(db, user, file_id)
    if item.status != "PARSED":
        raise ApiError("AI_REQUEST_INVALID", "file content is unavailable", 422)
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="FILE_READ", resource_type="FILE", resource_id=item.id, summary="file content read", trace_id=request.state.trace_id)
    db.commit()
    return success_response(request, {"id": str(item.id), "content": item.extracted_text or ""})


@router.delete("/{file_id}", summary="删除文件")
async def remove(file_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = owned_file(db, user, file_id)
    if db.scalar(select(message_file.c.message_id).where(message_file.c.file_id == item.id).limit(1)) is not None:
        raise ApiError("AI_RESOURCE_IN_USE", "file is still referenced by a message", 409)
    target = get_settings().file_storage_dir / item.storage_key
    target.unlink(missing_ok=True)
    item.status = "DELETED"
    item.extracted_text = None
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="FILE_DELETED", resource_type="FILE", resource_id=item.id, summary="file deleted", trace_id=request.state.trace_id)
    db.commit()
    return success_response(request, {"id": str(item.id), "status": item.status})