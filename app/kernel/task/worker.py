import asyncio
from uuid import UUID

from app.kernel.ports.queue_driver import Executor, QueueDriver


class Worker:
    def __init__(self, queue: QueueDriver, executor: Executor, concurrency: int = 10) -> None:
        self.queue = queue
        self.executor = executor
        self.semaphore = asyncio.Semaphore(concurrency)
        self._stopped = False

    async def run_once(self) -> None:
        task_id = await self.queue.get()
        try:
            async with self.semaphore:
                await self.executor(task_id)
        finally:
            self.queue.task_done()

    async def run(self) -> None:
        while not self._stopped:
            await self.run_once()

    def stop(self) -> None:
        self._stopped = True
