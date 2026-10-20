from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager


class LockProvider(ABC):
    """lock.provider Port 契约：命名互斥锁（契约 §3 任务认领防重）。

    acquire(name) 返回异步上下文管理器：进入即加锁、退出即释放（推荐唯一用法
    `async with provider.acquire(name):`）；release 为异常恢复路径的显式兜底。
    社区版内置进程内 asyncio 锁（单机语义诚实正确）；企业版可替换为
    DB / Redis 分布式锁实现。
    """

    @abstractmethod
    def acquire(self, name: str) -> AbstractAsyncContextManager:
        """获取命名锁；返回异步上下文管理器（async with 进入加锁，退出自动释放）。"""

    @abstractmethod
    def release(self, name: str) -> None:
        """显式释放命名锁（异常恢复兜底；正常路径经 async with 退出自动释放）。"""
