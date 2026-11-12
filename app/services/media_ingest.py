"""附件落库服务：文档解析 + 媒体原件存储。

约束：
- 走 ``object.store`` Port 流式写入（大小上限 + sha256 由实现承载），不直接拼接本地路径；
- 类型判定采用「扩展名 + 声明 MIME + 魔术字节」三方校验；声明 MIME 允许 ``image/*``
  与 ``application/octet-stream`` 这类不精确值，最终以扩展名与魔术字节为准；
- 文档类调用 ``parse_file``，成功 ``PARSED``、失败 ``PARSE_FAILED``；
  图片/音频/视频跳过解析，状态 ``UPLOADED``；
- 不修改 ``File`` 表结构，附件种类由调用方传入。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import ApiError
from app.core.ids import new_uuid7
from app.kernel.ports.object_store import ObjectStore
from app.models import File
from app.services.file_parser import parse_file

logger = logging.getLogger("z_torque.media_ingest")

MEDIA_KIND_IMAGE = "image"
MEDIA_KIND_FILE = "file"
MEDIA_KIND_AUDIO = "audio"
MEDIA_KIND_VIDEO = "video"
MEDIA_KINDS = frozenset({MEDIA_KIND_IMAGE, MEDIA_KIND_FILE, MEDIA_KIND_AUDIO, MEDIA_KIND_VIDEO})

_DOCUMENT_SUFFIXES = frozenset({".pdf", ".txt", ".md", ".csv", ".xlsx"})
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})
_AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".ogg", ".amr", ".m4a"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".webm"})

_SUFFIX_BY_KIND = {
    MEDIA_KIND_FILE: _DOCUMENT_SUFFIXES,
    MEDIA_KIND_IMAGE: _IMAGE_SUFFIXES,
    MEDIA_KIND_AUDIO: _AUDIO_SUFFIXES,
    MEDIA_KIND_VIDEO: _VIDEO_SUFFIXES,
}

_SUFFIX_BY_MIME = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/amr": ".amr",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}

_MIME_BY_SUFFIX = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".amr": "audio/amr",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}

# 不精确/通用声明值：不能作为拒绝依据，最终以扩展名 + 魔术字节判定
_TOLERATED_MIMES = frozenset({"application/octet-stream", "binary/octet-stream", "*/*"})


def media_kind_for_suffix(suffix: str) -> str | None:
    """按扩展名推断附件种类；未知扩展名返回 None。"""
    lowered = (suffix or "").lower()
    for kind, suffixes in _SUFFIX_BY_KIND.items():
        if lowered in suffixes:
            return kind
    return None


def signature_matches(suffix: str, head: bytes) -> bool:
    """魔术字节校验；文本类没有签名，返回 True。"""
    suffix = (suffix or "").lower()
    if suffix in {".txt", ".md", ".csv"}:
        return True
    if suffix == ".pdf":
        return head.startswith(b"%PDF")
    if suffix == ".xlsx":
        return head.startswith(b"PK\x03\x04")
    if suffix in {".jpg", ".jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if suffix == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix == ".gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if suffix == ".wav":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if suffix == ".mp3":
        return head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    if suffix == ".ogg":
        return head.startswith(b"OggS")
    if suffix == ".amr":
        return head.startswith(b"#!AMR")
    if suffix in {".mp4", ".m4a", ".mov"}:
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if suffix == ".webm":
        return head.startswith(b"\x1a\x45\xdf\xa3")
    return False


def _normalize_mime(mime_type: str | None) -> str:
    return (mime_type or "").split(";")[0].strip().lower()


def _is_imprecise_mime(declared: str) -> bool:
    return not declared or declared in _TOLERATED_MIMES or declared.endswith("/*")


def _mime_allowed(kind: str, mime_type: str | None) -> bool:
    """声明 MIME 与附件种类不得冲突；文档类不做严格 MIME 限制。"""
    declared = _normalize_mime(mime_type)
    if _is_imprecise_mime(declared):
        return True
    top = declared.split("/")[0]
    if kind == MEDIA_KIND_IMAGE:
        return top == "image"
    if kind == MEDIA_KIND_AUDIO:
        return top == "audio"
    if kind == MEDIA_KIND_VIDEO:
        return top == "video"
    return True


def _resolved_mime(kind: str, suffix: str, mime_type: str | None) -> str:
    declared = _normalize_mime(mime_type)
    if not _is_imprecise_mime(declared):
        return declared
    return _MIME_BY_SUFFIX.get(suffix) or {
        MEDIA_KIND_IMAGE: "image/*",
        MEDIA_KIND_AUDIO: "audio/*",
        MEDIA_KIND_VIDEO: "video/*",
    }.get(kind, "application/octet-stream")


def detect_suffix(head: bytes, kind: str, mime_type: str | None) -> str | None:
    """扩展名缺失/不可用时，按魔术字节或声明 MIME 推断真实后缀。"""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg" if kind == MEDIA_KIND_IMAGE else None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png" if kind == MEDIA_KIND_IMAGE else None
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif" if kind == MEDIA_KIND_IMAGE else None
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp" if kind == MEDIA_KIND_IMAGE else None
    if head.startswith(b"%PDF"):
        return ".pdf" if kind == MEDIA_KIND_FILE else None
    if head.startswith(b"PK\x03\x04"):
        return ".xlsx" if kind == MEDIA_KIND_FILE else None
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav" if kind == MEDIA_KIND_AUDIO else None
    if head.startswith(b"OggS"):
        return ".ogg" if kind == MEDIA_KIND_AUDIO else None
    if head.startswith(b"#!AMR"):
        return ".amr" if kind == MEDIA_KIND_AUDIO else None
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return ".mp3" if kind == MEDIA_KIND_AUDIO else None
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return {MEDIA_KIND_AUDIO: ".m4a", MEDIA_KIND_VIDEO: ".mp4", MEDIA_KIND_FILE: ".mp4"}.get(kind)
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return {MEDIA_KIND_VIDEO: ".webm", MEDIA_KIND_AUDIO: ".webm"}.get(kind)
    declared = _normalize_mime(mime_type)
    if not _is_imprecise_mime(declared):
        mapped = _SUFFIX_BY_MIME.get(declared)
        if mapped and mapped in _SUFFIX_BY_KIND[kind]:
            return mapped
        if kind == MEDIA_KIND_FILE and declared.startswith("text/"):
            return ".txt"
    return None


def _read_head(stream: BinaryIO) -> bytes:
    """读取头部用于签名校验，并尽量回到起始位置。"""
    head = stream.read(16)
    seek = getattr(stream, "seek", None)
    if callable(seek):
        try:
            seek(0)
        except (OSError, ValueError):
            pass
    return head or b""


def store_media(
    db: Session,
    store: ObjectStore,
    *,
    tenant_id: UUID,
    owner_user_id: UUID,
    conversation_id: UUID | None,
    file_name: str,
    media_type: str,
    stream: BinaryIO,
    max_bytes: int,
    mime_type: str | None = None,
) -> File:
    """校验并落库一个附件，返回未提交的 ``File``；失败抛 ``ApiError``。

    ``media_type`` 为附件种类（image/file/audio/video），``File.media_type`` 存真实 MIME。
    """
    kind = (media_type or "").strip().lower()
    if kind not in MEDIA_KINDS:
        raise ApiError("AI_FILE_TYPE_UNSUPPORTED", "unsupported attachment type", 415)
    safe_name = Path(file_name or "file").name[:255] or "file"
    suffix = Path(safe_name).suffix.lower()
    head = _read_head(stream)
    if suffix not in _SUFFIX_BY_KIND[kind]:
        # wechat-bot 等渠道可能落成 .bin（无扩展名），按魔术字节/声明 MIME 还原
        inferred = detect_suffix(head, kind, mime_type)
        if inferred is None:
            raise ApiError("AI_FILE_TYPE_UNSUPPORTED", "attachment extension is not supported", 415)
        suffix = inferred
        safe_name = f"{Path(safe_name).stem[: 255 - len(suffix)]}{suffix}"
    if not _mime_allowed(kind, mime_type):
        raise ApiError("AI_FILE_TYPE_UNSUPPORTED", "attachment mime type does not match its kind", 415)
    if not signature_matches(suffix, head):
        raise ApiError("AI_FILE_TYPE_UNSUPPORTED", "attachment content does not match its type", 415)

    key = str(new_uuid7())
    result = store.put(key, stream, max_bytes=max_bytes)
    target = store.local_path(key)
    item = File(
        id=new_uuid7(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        conversation_id=conversation_id,
        original_name=safe_name,
        media_type=_resolved_mime(kind, suffix, mime_type),
        size_bytes=result.size_bytes,
        sha256=result.sha256,
        storage_key=key,
        status="UPLOADED",
    )
    try:
        db.add(item)
        db.flush()
    except Exception:
        store.delete(key)
        raise
    if kind != MEDIA_KIND_FILE:
        return item
    try:
        item.extracted_text = parse_file(target, suffix, get_settings().file_max_extracted_text)
        item.status = "PARSED"
    except Exception as exc:  # noqa: BLE001 - 解析器第三方异常不可控，失败降级不阻断落库
        logger.warning("media ingest parse failed: id=%s suffix=%s err=%s", item.id, suffix, exc, exc_info=True)
        item.status = "PARSE_FAILED"
        item.parse_error = str(exc)[:500]
    return item


def media_kind_of(item: File) -> str:
    """按已落库 MIME 推断附件种类（读取侧使用）。"""
    mime = (item.media_type or "").lower()
    if mime.startswith("image/"):
        return MEDIA_KIND_IMAGE
    if mime.startswith("audio/"):
        return MEDIA_KIND_AUDIO
    if mime.startswith("video/"):
        return MEDIA_KIND_VIDEO
    return MEDIA_KIND_FILE


def file_public_metadata(item: File) -> dict[str, Any]:
    """附件种类与展示名（控制台气泡/模型降级提示共用）。"""
    return {"kind": media_kind_of(item), "name": item.original_name}
