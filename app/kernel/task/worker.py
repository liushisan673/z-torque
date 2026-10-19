import asyncio
import logging
from uuid import UUID

from app.kernel.ports.queue_driver import Executor, QueueDriver

logger = logging.getLogger("z_torque.task_worker")


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

    def stop(self) -> None:
        """请求 worker 在当前任务完成后退出循环（TaskRuntime.stop 先调它再 cancel）。"""
        self._stopped = True

    async def run(self) -> None:
        # 单个任务执行异常不得终止 worker 循环（否则后续任务将永久停留在队列中无人消费）
        while not self._stopped:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("task executor failed")
