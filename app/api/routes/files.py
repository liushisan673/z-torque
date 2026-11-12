import logging
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi import File as UploadFileParam
from fastapi.responses import FileResponse
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
from app.services.bridge_media import export_file_to_bridge
from app.services.file_parser import SUPPORTED, parse_file
from app.services.media_ingest import signature_matches

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
    # 与媒体落库共用同一套魔术字节判定，避免两处规则漂移
    return signature_matches(suffix, path.read_bytes()[:16])


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
    except Exception as exc:  # noqa: BLE001 - 解析器的第三方异常类型不可控，解析失败一律降级为 PARSE_FAILED，不阻断上传
        logger.warning("file parse failed: id=%s suffix=%s err=%s", file_id, suffix, exc, exc_info=True)
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


@router.get("/{file_id}/raw", summary="读取原始文件")
async def raw(file_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """返回 object.store 中的原始字节（图片/音频/视频预览与文件下载共用）。"""
    item = owned_file(db, user, file_id)
    if item.status == "DELETED":
        raise ApiError("AI_RESOURCE_NOT_FOUND", "file not found", 404)
    store = _object_store(request)
    target = store.local_path(item.storage_key)
    if not target.is_file():
        raise ApiError("AI_RESOURCE_NOT_FOUND", "file object not found", 404)
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="FILE_READ", resource_type="FILE", resource_id=item.id, summary="file raw content read", trace_id=request.state.trace_id)
    db.commit()
    return FileResponse(
        target,
        media_type=item.media_type or "application/octet-stream",
        filename=item.original_name,
        content_disposition_type="inline",
    )


@router.post("/{file_id}/bridge-export", summary="导出文件到 Bridge 共享卷")
async def bridge_export(file_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """把文件复制到 wechat-Bot 共享卷，返回 Bridge 容器内可发送路径。

    同机部署时，智能体可把返回的 ``bridge_path`` 作为 ``weixin.send_media`` 的
    ``url`` 参数，Bridge 会按容器内本地文件读取并发送到微信。
    """
    item = owned_file(db, user, file_id)
    if item.status == "DELETED":
        raise ApiError("AI_RESOURCE_NOT_FOUND", "file not found", 404)
    store = _object_store(request)
    exported = export_file_to_bridge(item, store, get_settings())
    record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="FILE_EXPORTED",
        resource_type="FILE",
        resource_id=item.id,
        summary="file exported to bridge shared volume",
        detail={"bridge_path": exported.bridge_path},
        trace_id=request.state.trace_id,
    )
    db.commit()
    return success_response(
        request,
        {
            "file_id": str(item.id),
            "bridge_path": exported.bridge_path,
            "media_type": exported.media_type,
            "file_name": exported.file_name,
            "size_bytes": exported.size_bytes,
            "sha256": exported.sha256,
            # 直接可用的工具调用参数，前端/智能体编排层可原样透传
            "send_media": {
                "tool": "weixin.send_media",
                "arguments": {
                    "media_type": exported.media_type,
                    "url": exported.bridge_path,
                    "file_name": exported.file_name,
                },
            },
        },
    )


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