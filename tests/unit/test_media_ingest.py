"""媒体落库服务：类型校验、文档解析、媒体跳过解析、大小限制。"""

from __future__ import annotations

import io
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.builtin_plugins.object_store import LocalObjectStore
from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.models import File, Tenant, User
from app.security.passwords import hash_password
from app.services.media_ingest import media_kind_of, signature_matches, store_media


@pytest.fixture()
def media_env(tmp_path):
    with SessionLocal() as db:
        tenant = Tenant(name=f"media-{uuid4()}")
        db.add(tenant)
        db.flush()
        owner = User(tenant_id=tenant.id, username=f"media-{uuid4()}", password_hash=hash_password("pass"))
        db.add(owner)
        db.commit()
        tenant_id, owner_id = tenant.id, owner.id
    yield db, LocalObjectStore(tmp_path / "objects"), tenant_id, owner_id
    # 只清理本用例租户，避免误删其他用例留下的父行导致 FK 失败
    with SessionLocal.begin() as db:
        db.execute(delete(File).where(File.tenant_id == tenant_id))
        db.execute(delete(User).where(User.tenant_id == tenant_id))
        db.execute(delete(Tenant).where(Tenant.id == tenant_id))


def _store(db, store, tenant_id, owner_id, *, name, kind, data, mime=None, limit=1024 * 1024):
    return store_media(
        db,
        store,
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        conversation_id=None,
        file_name=name,
        media_type=kind,
        stream=io.BytesIO(data),
        max_bytes=limit,
        mime_type=mime,
    )


def test_document_parsed_and_text_extracted(media_env):
    db, store, tenant_id, owner_id = media_env
    item = _store(db, store, tenant_id, owner_id, name="note.txt", kind="file", data="你好，世界".encode())
    db.commit()
    assert item.status == "PARSED"
    assert item.extracted_text == "你好，世界"
    assert item.media_type == "text/plain"
    assert media_kind_of(item) == "file"


def test_document_parse_failure_keeps_file(media_env):
    db, store, tenant_id, owner_id = media_env
    item = _store(db, store, tenant_id, owner_id, name="broken.xlsx", kind="file", data=b"PK\x03\x04broken")
    db.commit()
    assert item.status == "PARSE_FAILED"
    assert item.parse_error


def test_image_skips_parse(media_env):
    db, store, tenant_id, owner_id = media_env
    item = _store(db, store, tenant_id, owner_id, name="photo.jpg", kind="image", data=b"\xff\xd8\xff\xe0fakejpeg", mime="image/*")
    db.commit()
    assert item.status == "UPLOADED"
    assert item.media_type == "image/jpeg"
    assert media_kind_of(item) == "image"


def test_extensionless_media_infers_suffix(media_env):
    db, store, tenant_id, owner_id = media_env
    item = _store(
        db,
        store,
        tenant_id,
        owner_id,
        name="1794308365559-733f32b7.bin",
        kind="image",
        data=b"\xff\xd8\xff\xe0fakejpeg",
        mime="image/*",
    )
    db.commit()
    assert item.status == "UPLOADED"
    assert item.media_type == "image/jpeg"
    assert item.original_name.endswith(".jpg")


def test_oversize_rejected(media_env):
    db, store, tenant_id, owner_id = media_env
    with pytest.raises(ApiError) as exc:
        _store(db, store, tenant_id, owner_id, name="note.txt", kind="file", data=b"x" * 64, limit=8)
    assert exc.value.status_code == 413


def test_unsupported_extension_rejected(media_env):
    db, store, tenant_id, owner_id = media_env
    with pytest.raises(ApiError) as exc:
        _store(db, store, tenant_id, owner_id, name="app.exe", kind="file", data=b"MZ")
    assert exc.value.status_code == 415


def test_disguised_extension_rejected(media_env):
    db, store, tenant_id, owner_id = media_env
    with pytest.raises(ApiError) as exc:
        _store(db, store, tenant_id, owner_id, name="fake.jpg", kind="image", data=b"not an image")
    assert exc.value.status_code == 415


def test_mime_kind_conflict_rejected(media_env):
    db, store, tenant_id, owner_id = media_env
    with pytest.raises(ApiError) as exc:
        _store(db, store, tenant_id, owner_id, name="photo.png", kind="image", data=b"\x89PNG\r\n\x1a\nxxxx", mime="text/plain")
    assert exc.value.status_code == 415


def test_signature_matches_common_types():
    assert signature_matches(".pdf", b"%PDF-1.7")
    assert not signature_matches(".pdf", b"hello")
    assert signature_matches(".png", b"\x89PNG\r\n\x1a\n")
    assert signature_matches(".mp4", b"\x00\x00\x00\x18ftypmp42")
    assert signature_matches(".txt", b"anything")
