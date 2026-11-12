"""出站媒体：把 z-torque 的 File 导出到 Bridge 共享卷。

Bridge 容器只认两种媒体地址：容器内本地路径 ``filePath`` 或 http(s) ``url``。
同机部署时，把 z-torque 文件复制到 Bridge 已挂载的共享目录，最稳定：

    z-torque File (data/files/<storage_key>)
      -> 共享卷 outbound/<file_id>_<safe_name>
      -> Bridge 容器内 /tmp/weixin-agent/media/outbound/<file_id>_<safe_name>

导出的容器内路径可直接作为 ``weixin.send_media`` / ``weixin.send_message``
的 ``url`` 参数（Bridge 会把本地绝对路径当作 filePath）。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.core.errors import ApiError
from app.kernel.ports.object_store import ObjectStore
from app.models import File
from app.services.media_ingest import media_kind_of


@dataclass(frozen=True)
class BridgeMediaExport:
    file_id: str
    host_path: Path
    bridge_path: str
    file_name: str
    bridge_file_name: str
    media_type: str
    size_bytes: int
    sha256: str


def _shared_root(settings: Settings) -> Path:
    root = settings.bridge_media_root
    if root is None:
        raise ApiError(
            "AI_BRIDGE_MEDIA_UNAVAILABLE",
            "bridge shared media root is not configured",
            503,
        )
    return root.expanduser().resolve()


def _safe_export_name(item: File) -> str:
    """生成共享卷内的稳定文件名；去掉路径分隔符，避免逃逸。"""
    raw = Path(item.original_name or "file").name
    safe = raw.replace("\\", "_").replace("/", "_").strip() or "file"
    return f"{item.id}_{safe}"[:255]


def _ensure_within(root: Path, target: Path) -> Path:
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise ApiError("AI_BRIDGE_MEDIA_PATH_INVALID", "bridge media path escapes shared root", 500)
    return resolved


def bridge_path_for_source(source: str | None, settings: Settings) -> str | None:
    """把共享卷内的宿主机路径映射为 Bridge 容器内路径；不在共享卷时返回 None。

    入站 Webhook 的媒体通常已经在 Bridge 自己的媒体卷里（inbound_media_roots），
    优先映射到 ``bridge_media_inbound_container_root``，避免再复制一份；
    其他 z-torque 文件则映射到 ``bridge_media_container_root``（导出目录）。
    """
    if not source or source.startswith(("http://", "https://", "base64:")):
        return None
    try:
        path = Path(source).expanduser().resolve()
    except OSError:
        return None
    roots: list[tuple[Path, str]] = []
    for item in settings.inbound_media_roots:
        roots.append((Path(item).expanduser().resolve(), settings.bridge_media_inbound_container_root))
    bridge_root = settings.bridge_media_root
    if bridge_root is not None:
        roots.append((bridge_root.expanduser().resolve(), settings.bridge_media_container_root))
    for root, container_root in roots:
        if path == root or root in path.parents:
            relative = path.relative_to(root).as_posix()
            return f"{container_root.rstrip('/')}/{relative}"
    return None


def export_file_to_bridge(
    item: File,
    store: ObjectStore,
    settings: Settings,
) -> BridgeMediaExport:
    """把 ``File`` 复制到 Bridge 共享卷，返回容器内可发送路径。

    幂等：同一文件重复导出时，目标文件大小一致则跳过复制。
    """
    root = _shared_root(settings)
    source = store.local_path(item.storage_key)
    if not source.is_file():
        raise ApiError("AI_RESOURCE_NOT_FOUND", "file object not found", 404)

    subdir = settings.bridge_media_outbound_subdir.strip()
    out_dir = _ensure_within(root, root / subdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = _ensure_within(root, out_dir / _safe_export_name(item))

    if not (target.is_file() and target.stat().st_size == item.size_bytes):
        try:
            shutil.copyfile(source, target)
        except OSError as exc:
            raise ApiError("AI_FILE_EXPORT_FAILED", "failed to export file to bridge shared volume", 500) from exc

    bridge_path = f"{settings.bridge_media_container_root.rstrip('/')}/{subdir}/{target.name}"
    return BridgeMediaExport(
        file_id=str(item.id),
        host_path=target,
        bridge_path=bridge_path,
        file_name=item.original_name,
        bridge_file_name=target.name,
        media_type=media_kind_of(item),
        size_bytes=item.size_bytes,
        sha256=item.sha256 or "",
    )
