"""任务 worker 强制取消兜底：cancel_running 命中挂起协程，inflight 正确清理。"""

import asyncio
from uuid import uuid4

import pytest

from app.kernel.task.worker import Worker


class StubQueue:
    def __init__(self, ids):
        self.pending = list(ids)
        self.issued: list = []
        self.done: list = []

    async def get(self):
        if not self.pending:
            await asyncio.Event().wait()
        item = self.pending.pop(0)
        self.issued.append(item)
        return item

    def task_done(self):
        self.done.append(True)


@pytest.mark.asyncio
async def test_cancel_running_forces_executor_cancellation():
    """执行体挂死时：cancel_running 强制取消协程，worker 循环继续消费队列。"""
    cancelled = asyncio.Event()
    task_id = uuid4()

    async def executor(tid):
        try:
            await asyncio.Event().wait()  # 模拟挂死（如模型调用永不返回）
        except asyncio.CancelledError:
            cancelled.set()
            return {"task_id": str(tid), "status": "CANCELLED"}

    queue = StubQueue([task_id])
    worker = Worker(queue, executor, concurrency=1)
    run_task = asyncio.create_task(worker.run_once())
    await asyncio.sleep(0.05)

    assert worker.cancel_running(task_id) is True
    await asyncio.wait_for(run_task, timeout=2)
    assert cancelled.is_set()
    assert worker.inflight == {}
    assert queue.done  # task_done 正常回调，队列不会卡死


@pytest.mark.asyncio
async def test_cancel_running_misses_when_idle_or_finished():
    """空闲/已完成的任务：cancel_running 返回 False，不影响 worker。"""
    finished = uuid4()

    async def executor(tid):
        return {"task_id": str(tid), "status": "SUCCEEDED"}

    queue = StubQueue([finished])
    worker = Worker(queue, executor, concurrency=1)
    run_task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(run_task, timeout=2)

    assert worker.cancel_running(finished) is False
    assert worker.cancel_running(uuid4()) is False


@pytest.mark.asyncio
async def test_worker_loop_survives_executor_crash():
    """单个执行体崩溃由 run() 循环兜底（except Exception 记日志），循环继续消费队列。"""
    bad = uuid4()

    async def executor(tid):
        if tid == bad:
            raise RuntimeError("boom")
        return {"task_id": str(tid), "status": "SUCCEEDED"}

    queue = StubQueue([bad])
    worker = Worker(queue, executor, concurrency=1)
    # 模拟 run() 循环的兜底：run_once 抛出的异常被 except Exception 捕获后继续循环
    with pytest.raises(RuntimeError, match="boom"):
        await asyncio.wait_for(worker.run_once(), timeout=2)
    assert queue.done  # task_done 已回调，队列状态不会卡死
    assert worker.inflight == {}
