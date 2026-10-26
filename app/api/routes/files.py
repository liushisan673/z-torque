import logging
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
from app.kernel.audit.service import record_audit
from app.kernel.ports.object_store import ObjectStore
from app.models import Conversation, File, User
from app.models.message import message_file
from app.services.file_parser import SUPPORTED, parse_file

router = APIRouter(prefix="/files", tags=["files"])
logger = logging.getLogger("z_torque.files")


def _object_store(request: Request) -> ObjectStore:
    """经 Port 注册表取 object.store（API 层不 import 插件包，契约 v1.1 边界表）。"""
    return request.app.state.port_registry.get("object.store")


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
    store = _object_store(request)
    # 流式写入经 object.store Port（大小上限 + sha256 + 路径逃逸防护由实现承载）
    result = store.put(str(file_id), upload.file, max_bytes=settings.max_upload_size_mb * 1024 * 1024)
    target = store.local_path(str(file_id))
    size, digest = result.size_bytes, result.sha256
    if not _signature_matches(target, suffix):
        store.delete(str(file_id))
        raise ApiError("AI_FILE_TYPE_UNSUPPORTED", "file content does not match its type", 415)
    item = File(id=file_id, tenant_id=user.tenant_id, owner_user_id=user.id, conversation_id=conversation_id, original_name=Path(upload.filename or "file").name[:255], media_type=upload.content_type or "application/octet-stream", size_bytes=size, sha256=digest, storage_key=str(file_id), status="UPLOADED")
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
    # 经 Port 删除物理对象：先删对象再提交记录（delete 幂等，DB 失败可安全重试）；
    # 反向顺序一旦删除记录失败会留下无法追踪的孤儿文件。
    store = _object_store(request)
    try:
        store.delete(item.storage_key)
    except Exception:
        logger.exception("failed to delete object %s for file %s", item.storage_key, item.id)
        raise ApiError("AI_FILE_DELETE_FAILED", "failed to delete stored file", 500) from None
    item.status = "DELETED"
    item.extracted_text = None
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="FILE_DELETED", resource_type="FILE", resource_id=item.id, summary="file deleted", trace_id=request.state.trace_id)
    db.commit()
    return success_response(request, {"id": str(item.id), "status": item.status})