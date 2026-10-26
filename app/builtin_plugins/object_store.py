"""object.store Port 的内置实现：本地文件系统对象存储（契约 §3.5 对照表「本地文件系统」）。

流式写入（大小上限 + sha256 摘要）与路径逃逸防护语义自 app/api/routes/files.py
原地迁入；键即存储文件名。企业版可经 PortRegistry 以 PLUGIN/EXPLICIT 优先级
替换为 S3 / OSS 等对象存储（local_path 可实现为下载到临时文件）。
"""

import hashlib
from pathlib import Path
from typing import BinaryIO

from app.builtin_plugins.manifest import PluginManifest
from app.core.errors import ApiError
from app.kernel.ports.object_store import ObjectPutResult, ObjectStore

_CHUNK_SIZE = 1024 * 1024


class LocalObjectStore(ObjectStore):
    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        target = (self._root / key).resolve()
        if self._root not in target.parents:
            raise ApiError("AI_INTERNAL_ERROR", "invalid file storage path", 500)
        return target

    def put(self, key: str, data: BinaryIO, *, max_bytes: int) -> ObjectPutResult:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        try:
            with target.open("wb") as handle:
                while chunk := data.read(_CHUNK_SIZE):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ApiError("AI_FILE_TOO_LARGE", "file exceeds configured limit", 413)
                    digest.update(chunk)
                    handle.write(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return ObjectPutResult(size_bytes=size, sha256=digest.hexdigest())

    def local_path(self, key: str) -> Path:
        return self._resolve(key)

    def delete(self, key: str) -> None:
        self._resolve(key).unlink(missing_ok=True)


class ObjectStorePlugin:
    manifest = PluginManifest(
        name="object_store",
        description="本地文件系统对象存储（流式写入 + sha256 + 路径逃逸防护）",
        provides=("object.store",),
    )

    def contribute(self, sdk) -> None:
        root = sdk.settings.file_storage_dir
        sdk.register_port("object.store", lambda: LocalObjectStore(root), label="builtin_local_fs")
