from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from uuid import UUID


class QueueDriver(ABC):
    """queue.driver Port 契约：任务队列的进队 / 出队 / 确认语义。

    具体实现属能力实现（能力实现归插件）：社区版内置 InMemoryQueueDriver 位于
    app/builtin_plugins/queue_driver.py（G2d 迁入），由装配根经 PortRegistry 注入；
    企业版 multi-container/k8s 场景可提供 Redis / DB 后端实现，并经
    PortRegistry 以 PLUGIN/EXPLICIT 优先级替换，内核与 Worker 不感知后端。
    """

    @abstractmethod
    async def put(self, task_id: UUID) -> None: ...

    @abstractmethod
    async def get(self) -> UUID: ...

    @abstractmethod
    def task_done(self) -> None: ...


Executor = Callable[[UUID], Awaitable[dict]]
