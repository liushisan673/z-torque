"""出站媒体共享卷导出：路径映射、复制、幂等与路径逃逸。"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest

from app.builtin_plugins.object_store import LocalObjectStore
from app.config import get_settings
from app.core.errors import ApiError
from app.models import File
from app.services.bridge_media import bridge_path_for_source, export_file_to_bridge


def _store(tmp_path: Path, data: bytes) -> tuple[LocalObjectStore, File]:
    store = LocalObjectStore(tmp_path / "objects")
    result = store.put("object-key", io.BytesIO(data), max_bytes=1024 * 1024)
    item = File(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        conversation_id=None,
        original_name="photo.jpg",
        media_type="image/jpeg",
        size_bytes=result.size_bytes,
        sha256=result.sha256,
        storage_key="object-key",
        status="UPLOADED",
    )
    return store, item


def test_export_file_to_bridge_copies_and_returns_container_path(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "bridge_media_shared_root", tmp_path / "shared")
    monkeypatch.setattr(settings, "bridge_media_container_root", "/tmp/weixin-agent/media")
    monkeypatch.setattr(settings, "bridge_media_outbound_subdir", "outbound")
    data = b"\xff\xd8\xff\xe0fakejpeg"
    store, item = _store(tmp_path, data)

    exported = export_file_to_bridge(item, store, settings)

    assert exported.media_type == "image"
    assert exported.bridge_path == f"/tmp/weixin-agent/media/outbound/{item.id}_photo.jpg"
    assert exported.host_path.read_bytes() == data
    assert exported.host_path.parent == (tmp_path / "shared" / "outbound").resolve()

    # 幂等：重复导出复用同一路径，不报错
    again = export_file_to_bridge(item, store, settings)
    assert again.bridge_path == exported.bridge_path


def test_export_file_to_bridge_strips_path_segments(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "bridge_media_shared_root", tmp_path / "shared")
    monkeypatch.setattr(settings, "bridge_media_container_root", "/tmp/weixin-agent/media")
    monkeypatch.setattr(settings, "bridge_media_outbound_subdir", "outbound")
    store, item = _store(tmp_path, b"\xff\xd8\xff\xe0fakejpeg")
    item.original_name = "..\\..\\evil.jpg"

    exported = export_file_to_bridge(item, store, settings)

    assert exported.host_path.parent == (tmp_path / "shared" / "outbound").resolve()
    assert ".." not in exported.host_path.name


def test_export_file_to_bridge_requires_shared_root(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "bridge_media_shared_root", None)
    monkeypatch.setattr(settings, "inbound_media_roots", [])
    store, item = _store(tmp_path, b"\xff\xd8\xff\xe0fakejpeg")

    with pytest.raises(ApiError) as exc:
        export_file_to_bridge(item, store, settings)
    assert exc.value.code == "AI_BRIDGE_MEDIA_UNAVAILABLE"


def test_bridge_path_for_source_maps_only_shared_root(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "bridge_media_shared_root", tmp_path / "shared")
    monkeypatch.setattr(settings, "bridge_media_container_root", "/tmp/weixin-agent/media")
    inside = tmp_path / "shared" / "inbound" / "a.jpg"

    assert bridge_path_for_source(str(inside), settings) == "/tmp/weixin-agent/media/inbound/a.jpg"
    assert bridge_path_for_source("https://example.com/a.jpg", settings) is None
    assert bridge_path_for_source(str(tmp_path / "other" / "a.jpg"), settings) is None
