import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from uuid import UUID


class QueueDriver(ABC):
    """queue.driver Port 契约：任务队列的进队 / 出队 / 确认语义。

    社区版内置 InMemoryQueueDriver（单机形态，恢复依赖 Outbox 补偿）；
    企业版 multi-container/k8s 场景可提供 Redis / DB 后端实现，并经
    PortRegistry 以 PLUGIN/EXPLICIT 优先级替换，内核与 Worker 不感知后端。
    """

    @abstractmethod
    async def put(self, task_id: UUID) -> None: ...

    @abstractmethod
    async def get(self) -> UUID: ...

    @abstractmethod
    def task_done(self) -> None: ...


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


Executor = Callable[[UUID], Awaitable[dict]]
