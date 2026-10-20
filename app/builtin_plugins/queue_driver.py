"""任务队列能力实现（queue.driver Port，G2d 迁入）。

InMemoryQueueDriver 属能力实现（能力实现归插件）；本模块按插件→内核方向
引用 Port 契约（app/kernel/ports/queue_driver.py 的 QueueDriver ABC）。
内核 `task/runtime` 仅依赖 QueueDriver 契约，队列实例由装配根经
PortRegistry（registry.get("queue.driver")）注入；企业版后端（Redis/DB）
可以新插件实现同一契约并以 PLUGIN/EXPLICIT 优先级替换，内核不感知。
"""

import asyncio
from uuid import UUID

from app.kernel.ports.queue_driver import QueueDriver


class InMemoryQueueDriver(QueueDriver):
    """进程内 asyncio 队列实现（社区版默认）。"""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[UUID] = asyncio.Queue()

    async def put(self, task_id: UUID) -> None:
        await self.queue.put(task_id)

    async def get(self) -> UUID:
        return await self.queue.get()

    def task_done(self) -> None:
        self.queue.task_done()
