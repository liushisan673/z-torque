import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID


class QueueDriver:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[UUID] = asyncio.Queue()

    async def put(self, task_id: UUID) -> None:
        await self.queue.put(task_id)

    async def get(self) -> UUID:
        return await self.queue.get()

    def task_done(self) -> None:
        self.queue.task_done()


Executor = Callable[[UUID], Awaitable[dict]]
