"""lock.provider Port 的内置实现：进程内 asyncio 命名锁注册表。

社区版单机形态下的诚实实现：同名锁互斥（跨协程），退出自动释放；
企业版可经 PortRegistry 以 PLUGIN/EXPLICIT 优先级替换为 DB / Redis 分布式锁。
"""

import asyncio
import threading

from contextlib import AbstractAsyncContextManager

from app.builtin_plugins.manifest import PluginManifest
from app.kernel.ports.lock_provider import LockProvider
from app.kernel.registry.port_registry import PortPriority


class InProcessLockProvider(LockProvider):
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        # 注册表守卫：acquire 可能在多个事件循环中被调用，创建动作需线程安全
        self._registry_guard = threading.Lock()

    def acquire(self, name: str) -> AbstractAsyncContextManager:
        with self._registry_guard:
            lock = self._locks.get(name)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[name] = lock
        return lock

    def release(self, name: str) -> None:
        """异常恢复兜底：仅当锁当前被持有时释放；正常路径应经 async with 退出释放。"""
        with self._registry_guard:
            lock = self._locks.get(name)
        if lock is not None and lock.locked():
            try:
                lock.release()
            except RuntimeError:
                pass  # 无等待者/竞态下已释放，按幂等处理


class LockProviderPlugin:
    manifest = PluginManifest(
        name="lock_provider",
        description="进程内命名互斥锁（asyncio.Lock 注册表，Outbox 认领防重）",
        provides=("lock.provider",),
    )

    def contribute(self, sdk) -> None:
        sdk.register_port("lock.provider", InProcessLockProvider, label="builtin_inprocess_lock")
