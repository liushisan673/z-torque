from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class ObjectPutResult:
    size_bytes: int
    sha256: str


class ObjectStore(ABC):
    """object.store Port 契约：文件对象存储（契约 §3 信息族）。

    流式写入（含大小上限与摘要计算）+ 按键取本地路径 / 删除；社区版内置本地
    文件系统（settings.file_storage_dir，键即文件名，含路径逃逸防护），企业版
    可替换为 S3 / OSS 等对象存储（local_path 可实现为下载到临时文件）。
    """

    @abstractmethod
    def put(self, key: str, data: BinaryIO, *, max_bytes: int) -> ObjectPutResult:
        """流式写入对象；超限抛 AI_FILE_TOO_LARGE(413)，返回字节数与 sha256 摘要。"""

    @abstractmethod
    def local_path(self, key: str) -> Path:
        """对象本地路径（供解析器等需要文件路径的消费者使用；防路径逃逸）。"""

    @abstractmethod
    def delete(self, key: str) -> None:
        """删除对象；键不存在时静默（幂等）。"""
