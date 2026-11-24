"""阶段13 第5批（执行可见性 + 延迟优化）回归：

1. 决策轮种子：/chat/stream 已经拿到 tool_calls 时，worker 不得再推理同一轮（省一整轮模型延迟）。
2. 过程事件即时提交：执行中的事件必须能被另一个会话立刻读到，否则前端只能等任务结束才看到全部过程。
"""

import asyncio
from uuid import uuid4

from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.kernel.model_gateway import ModelMessage, ModelRequest, ModelResponse, ModelToolCall
from app.kernel.ports.skill_executor import ExecutionResult
from app.kernel.security_context import SecurityContext
from app.kernel.task.checkpoints import create_seed_checkpoint, take_seed_checkpoint
from app.kernel.task.service import append_task_event, create_task
from app.models import (
    Agent,
    AgentVersion,
    Conversation,
    Task,
    TaskCheckpoint,
    TaskEvent,
    Tenant,
    User,
)
from app.security.passwords import hash_password
from app.services.execution_loop import AgentExecutionLoop, seed_round, serialize_seed
from app.services.function_calling import ToolMapping
from app.services.task_stream import task_event_stream


def _fixture(db):
    tenant = Tenant(name=f"seed-{uuid4()}")
    db.add(tenant)
    db.flush()
    user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
    db.add(user)
    db.flush()
    agent = Agent(tenant_id=tenant.id, owner_user_id=user.id, name="agent", source_type="USER", status="ACTIVE")
    db.add(agent)
    db.flush()
    version = AgentVersion(tenant_id=tenant.id, agent_id=agent.id, version_number=1, status="PUBLISHED", config_schema_version="1", config_snapshot={"system_prompt": "test"})
    db.add(version)
    db.flush()
    agent.current_published_version_id = version.id
    conversation = Conversation(tenant_id=tenant.id, owner_user_id=user.id, agent_id=agent.id, status="ACTIVE")
    db.add(conversation)
    db.flush()
    return tenant, user, agent, version, conversation


def _cleanup():
    with SessionLocal.begin() as db:
        for model in (TaskEvent, TaskCheckpoint, Task, Conversation, AgentVersion, Agent, User, Tenant):
            db.execute(delete(model))


def _mappings() -> tuple[dict[str, ToolMapping], dict]:
    mapping = ToolMapping("orders_query", uuid4(), uuid4(), {"type": "object", "properties": {}})
    return {"orders_query": mapping}, mapping


def _request(tool: str) -> ModelRequest:
    return ModelRequest(
        trace_id="trace-seed",
        messages=[ModelMessage("system", "sys"), ModelMessage("user", "查一下订单")],
        model="test-model",
        tools=[{"type": "function", "function": {"name": tool, "parameters": {"type": "object", "properties": {}}}}],
        timeout_seconds=5,
    )


def test_seed_round_rejects_unknown_tool():
    """工具已解绑时种子必须失效：不能拿旧决策去调用不再存在的工具。"""
    mappings, mapping = _mappings()
    seed = serialize_seed(
        messages=[ModelMessage("user", "hi")],
        response=ModelResponse(tool_calls=[ModelToolCall("call-1", "orders_query", {})]),
    )
    restored = seed_round(seed, mappings)
    assert restored is not None
    assert restored[1].tool_calls[0].name == "orders_query"

    other_mappings = {mapping.model_name + "_renamed": mapping}
    assert seed_round(seed, other_mappings) is None
    assert seed_round(None, mappings) is None
    assert seed_round({"messages": "broken"}, mappings) is None


def test_seed_checkpoint_is_taken_once():
    with SessionLocal() as db:
        tenant, user, agent, version, conversation = _fixture(db)
        task = create_task(
            db,
            ctx=SecurityContext(tenant_id=tenant.id, user_id=user.id),
            agent_id=agent.id,
            agent_version_id=version.id,
            idempotency_key=str(uuid4()),
            request_summary="查订单",
            conversation_id=conversation.id,
            message_id=None,
        )
        create_seed_checkpoint(db, task_id=task.id, tenant_id=tenant.id, idempotency_key="seed-1", seed={"messages": [], "assistant": {}})
        db.commit()
        assert take_seed_checkpoint(db, task.id) is not None
        db.commit()
        # 第二次取不到：同一任务只消费一次，且行已删除（task_id 唯一约束不能被种子长期占住）
        assert take_seed_checkpoint(db, task.id) is None
        assert db.scalar(select(TaskCheckpoint).where(TaskCheckpoint.task_id == task.id)) is None
        _cleanup()


def test_loop_skips_duplicate_decision_round_and_commits_progress_events():
    """种子命中时只推理一次（收尾轮），且工具事件在独立会话里立刻可见。"""
    _cleanup()
    with SessionLocal() as db:
        tenant, user, agent, version, conversation = _fixture(db)
        task = create_task(
            db,
            ctx=SecurityContext(tenant_id=tenant.id, user_id=user.id),
            agent_id=agent.id,
            agent_version_id=version.id,
            idempotency_key=str(uuid4()),
            request_summary="查订单",
            conversation_id=conversation.id,
            message_id=None,
        )
        db.commit()

        mappings, _ = _mappings()
        seed = serialize_seed(
            messages=[ModelMessage("system", "sys"), ModelMessage("user", "查一下订单")],
            response=ModelResponse(tool_calls=[ModelToolCall("call-1", "orders_query", {"limit": 3})], finish_reason="tool_calls"),
        )

        class FakeGateway:
            def __init__(self) -> None:
                self.rounds: list[int] = []

            async def generate(self, request: ModelRequest) -> ModelResponse:
                self.rounds.append(request.round_number)
                return ModelResponse(content="订单共 3 条")

        class FakeSkillGateway:
            def __init__(self) -> None:
                self.calls = 0

            async def execute_async(self, db, ctx, **kwargs):
                self.calls += 1
                return ExecutionResult(status="SUCCEEDED", data={"rows": 3})

        gateway = FakeGateway()
        skill_gateway = FakeSkillGateway()
        loop = AgentExecutionLoop(gateway, skill_gateway)
        result = asyncio.run(
            loop.run(
                request=_request("orders_query"),
                mappings=mappings,
                db=db,
                ctx=SecurityContext(tenant_id=tenant.id, user_id=user.id),
                task_id=task.id,
                seed=seed,
            )
        )
        # 决策轮被种子替代：只发生第 2 轮推理，工具恰好执行一次
        assert gateway.rounds == [2]
        assert skill_gateway.calls == 1
        assert result.response.content == "订单共 3 条"

        # 独立会话立即读到过程事件（提交过），顺序为 工具调用 → 工具结果 → 模型推理开始
        with SessionLocal() as observer:
            rows = observer.scalars(
                select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.seq)
            ).all()
        event_types = [row.event_type for row in rows]
        assert event_types == ["TASK_CREATED", "TASK_TOOL_CALL", "TASK_TOOL_RESULT", "TASK_MODEL_STARTED"]
        assert rows[1].payload_summary["tool_name"] == "orders_query"


def test_task_stream_emits_events_written_after_subscription():
    """前端订阅建立之后写入的过程事件，必须在同一条 SSE 连接里推出去。

    这是「执行中只看到『执行中』、等任务结束才刷出全部步骤」的 SSE 侧回归：
    长连接持有自己的 DB 会话，只有 worker 真的提交了事件，下一次轮询才拿得到。
    """
    _cleanup()
    with SessionLocal() as db:
        tenant, user, agent, version, conversation = _fixture(db)
        task = create_task(
            db,
            ctx=SecurityContext(tenant_id=tenant.id, user_id=user.id),
            agent_id=agent.id,
            agent_version_id=version.id,
            idempotency_key=str(uuid4()),
            request_summary="查订单",
            conversation_id=conversation.id,
            message_id=None,
        )
        db.commit()

        async def drive() -> list[str]:
            chunks: list[str] = []
            with SessionLocal() as reader:
                stream = task_event_stream(reader, task_id=task.id, tenant_id=tenant.id)
                chunks.append(await anext(stream))  # task.snapshot
                # 模拟 worker：订阅建立之后才提交一条过程事件
                with SessionLocal() as writer:
                    live_task = writer.get(Task, task.id)
                    append_task_event(writer, live_task, "TASK_TOOL_CALL", "calling tool orders_query", payload_summary={"tool_name": "orders_query"})
                    writer.commit()
                # 历史事件回放（TASK_CREATED）在前，随后轮询应推出新提交的事件
                for _ in range(5):
                    chunks.append(await anext(stream))
                    if any("TASK_TOOL_CALL" in chunk for chunk in chunks):
                        break
                await stream.aclose()
            return chunks

        chunks = asyncio.run(drive())
        assert "task.snapshot" in chunks[0]
        assert any("event: TASK_TOOL_CALL" in chunk for chunk in chunks[1:])
        assert any("orders_query" in chunk for chunk in chunks[1:])
        _cleanup()

        _cleanup()
