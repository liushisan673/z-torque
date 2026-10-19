from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.kernel.task.service import create_task
from app.models import Agent, AgentVersion, Conversation, Message, Tenant, User
from app.security.passwords import hash_password
from app.services.chat_application import ensure_conversation_title
from app.services.chat_service import handle_mock_chat


def _fixture(db):
    tenant = Tenant(name=f"stage3-{uuid4()}")
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


def test_mock_chat_does_not_create_task():
    with SessionLocal() as db:
        tenant, user, _, _, conversation = _fixture(db)
        result = handle_mock_chat(db, tenant_id=tenant.id, user_id=user.id, conversation_id=conversation.id, content="hello")
        assert result.task_id is None
        assert result.assistant_message is not None
        assert db.scalar(select(Message).where(Message.id == result.user_message.id)).task_id is None
        db.rollback()


def test_auto_conversation_title():
    # 首条消息自动命名：压缩空白、超长截断加省略号
    conv = Conversation()
    ensure_conversation_title(conv, "  帮我查一下\n\n最近的订单   状态  ")
    assert conv.title == "帮我查一下 最近的订单 状态"
    long_conv = Conversation()
    ensure_conversation_title(long_conv, "一" * 40)
    assert long_conv.title == "一" * 30 + "…"
    # 已有标题（手动重命名）不覆盖；空内容不生成
    named = Conversation(title="我的会话")
    ensure_conversation_title(named, "新消息")
    assert named.title == "我的会话"
    empty = Conversation()
    ensure_conversation_title(empty, "   \n  ")
    assert empty.title is None


def test_child_agent_delegation_uses_mapping_builder(monkeypatch):
    """回归：委派子代理必须走独立 build_tool_mappings。

    曾因调用 Port 改造时已迁除的 TaskRuntime._build_tool_mappings 抛 AttributeError，
    任务在模型返回委派调用后 1 秒内以 AI_TASK_FAILED 兜底失败。
    """
    import asyncio
    from types import SimpleNamespace

    from app.kernel.model_gateway import ModelResponse
    from app.services import execution_loop

    calls: dict = {}

    def fake_builder(db, version, tenant_id):
        calls["version"] = version
        return {}, []  # 无工具 → 走 generate 分支

    monkeypatch.setattr(execution_loop, "build_tool_mappings", fake_builder)

    class FakeGateway:
        async def generate(self, request):
            return ModelResponse(content="child ok")

    child_version = SimpleNamespace(id=uuid4(), tenant_id="t", status="PUBLISHED", agent_id="a", config_snapshot={})
    child_agent = SimpleNamespace(status="ACTIVE")

    class FakeDB:
        def get(self, model, key):
            if model.__name__ == "AgentVersion":
                return child_version
            if model.__name__ == "Agent":
                return child_agent
            return None

    loop = execution_loop.AgentExecutionLoop(FakeGateway(), None)
    out = asyncio.run(loop._run_child_agent(
        FakeDB(), tenant_id=child_version.tenant_id, user_id="u",
        child_agent_version_id=child_version.id, task_text="do it",
        delegation_depth=0, cancellation_event=None, deadline_at=None,
    ))
    assert out == "child ok"
    assert calls["version"] is child_version


def test_fail_model_task_writes_assistant_bubble_not_user_flag():
    """失败收尾：模型气泡承载失败说明（幂等），触发消息不被标记失败。"""
    from app.kernel.task.runtime import TaskRuntime
    from app.kernel.task.states import TaskStatus

    with SessionLocal() as db:
        tenant, user, agent, version, conversation = _fixture(db)
        trigger = Message(tenant_id=tenant.id, conversation_id=conversation.id, sender_type="USER", content="hello", status="PROCESSING")
        db.add(trigger)
        db.flush()
        task = create_task(db, tenant_id=tenant.id, owner_user_id=user.id, agent_id=agent.id, agent_version_id=version.id, idempotency_key=str(uuid4()), request_summary="hello", conversation_id=conversation.id, message_id=trigger.id)
        task.status = TaskStatus.RUNNING
        db.commit()

        result = TaskRuntime._fail_model_task(db, task, "AI_TEST", "boom")
        assert result["status"] == "FAILED"

        bubble = db.query(Message).filter(Message.task_id == task.id, Message.sender_type == "ASSISTANT").first()
        assert bubble is not None
        assert bubble.status == "FAILED" and bubble.error_code == "AI_TEST" and "boom" in bubble.content
        db.refresh(trigger)
        assert trigger.status == "PROCESSING"  # 用户气泡不承载执行失败

        # 幂等：重复收尾不产生第二条失败气泡
        TaskRuntime._fail_model_task(db, task, "AI_TEST", "boom")
        count = db.query(Message).filter(Message.task_id == task.id, Message.sender_type == "ASSISTANT").count()
        assert count == 1

        # 清理审计残留：test_task_kernel 的共享清理链不含 zr_audit_log，避免 FK 阻塞其 teardown
        from app.models import AuditLog
        db.query(AuditLog).filter(AuditLog.resource_type == "TASK", AuditLog.resource_id == task.id).delete()
        db.commit()


def test_trigger_alias_mapping_and_conflict():
    with SessionLocal() as db:
        tenant, user, agent, version, conversation = _fixture(db)
        message = Message(tenant_id=tenant.id, conversation_id=conversation.id, sender_type="USER", content="run", status="COMPLETED")
        db.add(message)
        db.flush()
        task = create_task(db, tenant_id=tenant.id, owner_user_id=user.id, agent_id=agent.id, agent_version_id=version.id, idempotency_key=str(uuid4()), request_summary="run", conversation_id=conversation.id, message_id=message.id)
        assert task.trigger_message_id == message.id
        assert task.message_id == message.id
        # 设计约定（task/service.py）：trigger 消息不回写 task_id（zr_message.task_id 的
        # UNIQUE 约束保留给任务产出的 ASSISTANT 回复）；关联由 Task.trigger_message_id 承载
        assert message.task_id is None
        with pytest.raises(ApiError) as error:
            create_task(db, tenant_id=tenant.id, owner_user_id=user.id, agent_id=agent.id, agent_version_id=version.id, idempotency_key=str(uuid4()), request_summary="run", conversation_id=conversation.id, trigger_message_id=message.id, message_id=uuid4())
        assert error.value.code == "AI_REQUEST_INVALID"
        db.rollback()