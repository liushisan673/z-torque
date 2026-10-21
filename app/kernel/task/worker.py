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
        # 执行中的协程映射（task_id → asyncio.Task）：支持"立即取消"兜底——
        # 协作式取消依赖执行体在 await 边界检查事件，执行体挂死时事件永远不被检查
        self.inflight: dict[UUID, asyncio.Task] = {}

    async def run_once(self) -> None:
        task_id = await self.queue.get()
        try:
            async with self.semaphore:
                inner = asyncio.create_task(self.executor(task_id))
                self.inflight[task_id] = inner
                try:
                    await inner
                except asyncio.CancelledError:
                    # 被取消的是 inner（强制取消兜底），不是 worker 循环本身；
                    # inner 自身的 _execute 已把任务收敛为 CANCELLED，这里吞掉继续消费队列
                    if not inner.cancelled():
                        raise
                finally:
                    self.inflight.pop(task_id, None)
        finally:
            self.queue.task_done()

    def cancel_running(self, task_id: UUID) -> bool:
        """强制取消执行中的任务协程；返回是否命中。协作式取消无响应时的兜底。"""
        inner = self.inflight.get(task_id)
        if inner is not None and not inner.done():
            inner.cancel()
            return True
        return False

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
