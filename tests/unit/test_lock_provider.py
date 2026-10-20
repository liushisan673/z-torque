"""lock.provider Port 收口测试（P1-4：包装式收口，不动并发语义）。"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import delete, text

from app.builtin_plugins.lock_provider import InProcessLockProvider, LockProviderPlugin
from app.builtin_plugins.manifest import PluginSdk
from app.db.session import SessionLocal
from app.kernel.registry.port_registry import PortRegistry, PortState
from app.kernel.ports.specs import PORT_SPECS
from app.kernel.task.dispatcher import OutboxDispatcher
from app.models import Agent, AgentVersion, AuthSession, Outbox, Task, TaskEvent, Tenant, User
from app.security.passwords import hash_password


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with SessionLocal.begin() as db:
        # 全局清理会触碰其他用例残留的外键引用行，沿用 test_auth_flow 的 FK-OFF 模式
        db.execute(text("PRAGMA foreign_keys=OFF"))
        db.execute(delete(TaskEvent))
        db.execute(delete(Outbox))
        db.execute(delete(Task))
        db.execute(delete(AgentVersion))
        db.execute(delete(Agent))
        db.execute(delete(AuthSession))
        db.execute(delete(User))
        db.execute(delete(Tenant))
        db.execute(text("PRAGMA foreign_keys=ON"))


def test_named_locks_are_mutually_exclusive():
    provider = InProcessLockProvider()
    order: list[str] = []

    async def holder(name: str, gate: asyncio.Event, release: asyncio.Event):
        async with provider.acquire(name):
            order.append(f"enter-{name}")
            gate.set()
            await release.wait()
            order.append(f"exit-{name}")

    async def scenario():
        gate_a, release_a = asyncio.Event(), asyncio.Event()
        release_b = asyncio.Event()
        task_a = asyncio.create_task(holder("outbox.dispatch", gate_a, release_a))
        await gate_a.wait()
        task_b = asyncio.create_task(holder("outbox.dispatch", asyncio.Event(), release_b))
        await asyncio.sleep(0.05)
        assert order == ["enter-outbox.dispatch"]  # B 被 A 阻塞
        release_a.set()
        await task_a
        release_b.set()
        await task_b
        assert order == ["enter-outbox.dispatch", "exit-outbox.dispatch", "enter-outbox.dispatch", "exit-outbox.dispatch"]

    asyncio.run(scenario())


def test_different_names_do_not_block_each_other():
    provider = InProcessLockProvider()
    entered: list[str] = []

    async def scenario():
        gates = [asyncio.Event(), asyncio.Event()]

        async def holder(name: str, gate: asyncio.Event):
            async with provider.acquire(name):
                entered.append(name)
                gate.set()

        tasks = [asyncio.create_task(holder(f"lock-{i}", gates[i])) for i in range(2)]
        await asyncio.gather(*tasks)
        assert sorted(entered) == ["lock-0", "lock-1"]

    asyncio.run(scenario())


def test_release_is_idempotent_escape_hatch():
    provider = InProcessLockProvider()
    provider.release("never-locked")  # 未加锁释放按幂等处理，不抛异常

    async def scenario():
        lock = provider.acquire("x")
        await lock.__aenter__()
        provider.release("x")  # 显式兜底释放
        assert provider._locks["x"].locked() is False

    asyncio.run(scenario())


def test_lock_plugin_registers_port():
    spec = next(s for s in PORT_SPECS if s.name == "lock.provider")
    registry = PortRegistry((spec,))
    LockProviderPlugin().contribute(PluginSdk.of(LockProviderPlugin(), registry=registry))
    registry.resolve()
    resolution = registry.resolution("lock.provider")
    assert resolution.state == PortState.RESOLVED
    assert resolution.plugin == "lock_provider"
    assert isinstance(resolution.instance, InProcessLockProvider)


def test_dispatcher_with_lock_provider_still_dispatches():
    """接入 lock.provider 后认领-派发行为不变（包装式收口回归）。"""
    from app.builtin_plugins.queue_driver import InMemoryQueueDriver
    from app.kernel.security_context import SecurityContext
    from app.kernel.task.service import create_task

    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"lockp-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        task = create_task(db, ctx=SecurityContext(tenant_id=tenant.id, user_id=user.id), agent_id=uuid4(), agent_version_id=uuid4(), idempotency_key=str(uuid4()), request_summary="run")

    async def scenario():
        queue = InMemoryQueueDriver()
        dispatcher = OutboxDispatcher(queue, lock_provider=InProcessLockProvider())
        with SessionLocal() as db:
            assert await dispatcher.dispatch_once(db) is True
            row = db.query(Outbox).filter_by(aggregate_id=task.id).one()
            assert row.status == "SENT"
            assert await queue.get() == task.id  # 任务已入队（PENDING → QUEUED）

    asyncio.run(scenario())
