import asyncio
from types import SimpleNamespace
from uuid import uuid4

from app.kernel.model_gateway import ModelResponse
from app.kernel.security_context import SecurityContext
from app.services import execution_loop


def test_child_agent_inherits_scoped_memory(monkeypatch):
    captured: dict = {}
    tenant_id = uuid4()
    user_id = uuid4()
    child_id = uuid4()
    conversation_id = uuid4()

    child_version = SimpleNamespace(id=child_id, tenant_id=tenant_id, status="PUBLISHED", agent_id=uuid4(), config_snapshot={})
    child_agent = SimpleNamespace(status="ACTIVE")
    parent_task = SimpleNamespace(conversation_id=conversation_id)

    class FakeDB:
        def get(self, model, key):
            if model.__name__ == "AgentVersion":
                return child_version
            if model.__name__ == "Agent":
                return child_agent
            if model.__name__ == "Task":
                return parent_task
            return None

        def scalar(self, *args, **kwargs):
            return None

        def scalars(self, *args, **kwargs):
            return None

    def fake_bundle(db, **kwargs):
        captured["memory_kwargs"] = kwargs
        return {"USER:profile": "用户偏好：简洁回答", "AGENT:rule": "使用中文"}

    monkeypatch.setattr(execution_loop, "call_optional", fake_bundle)
    monkeypatch.setattr(execution_loop, "build_tool_mappings", lambda *args, **kwargs: ({}, []))
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(
        delegation_max_depth=1, model_name="global-model", model_temperature=0.9,
        model_max_tokens=4096, model_timeout_seconds=60, memory_long_enabled=True,
    ))

    class FakeGateway:
        async def generate(self, request):
            captured["messages"] = request.messages
            return ModelResponse(content="child ok")

    loop = execution_loop.AgentExecutionLoop(FakeGateway(), None)
    result = asyncio.run(loop._run_child_agent(
        FakeDB(), ctx=SecurityContext(tenant_id=tenant_id, user_id=user_id, trace_id="d1"),
        child_agent_version_id=child_id, task_text="do it", delegation_depth=0,
        cancellation_event=None, deadline_at=None, task_id=uuid4(),
    ))

    assert result["status"] == "SUCCEEDED"
    assert result["content"] == "child ok"
    assert captured["memory_kwargs"]["user_id"] == user_id
    assert captured["memory_kwargs"]["agent_id"] == child_version.agent_id
    assert captured["memory_kwargs"]["conversation_id"] == conversation_id
    contents = [message.content for message in captured["messages"]]
    assert any("Delegated memory / 用户画像" in text and "用户偏好：简洁回答" in text for text in contents)
    assert any("Delegated memory / 智能体指令" in text for text in contents)



def test_a2a_dispatch_requires_invoke_permission(monkeypatch):
    tenant_id = uuid4()
    user_id = uuid4()
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(delegation_max_depth=3),
    )
    monkeypatch.setattr(
        "app.security.permissions.list_user_permissions",
        lambda *_args, **_kwargs: set(),
    )
    loop = execution_loop.AgentExecutionLoop(None, None)
    result = asyncio.run(loop._run_child_agent(
        object(),
        ctx=SecurityContext(tenant_id=tenant_id, user_id=user_id, trace_id="a2a-denied"),
        child_agent_version_id=uuid4(),
        task_text="perform a delegated task",
        delegation_depth=0,
        require_current_version=True,
        cancellation_event=None,
        deadline_at=None,
    ))
    assert result["status"] == "FAILED"
    assert result["error_code"] == "AI_A2A_DENIED"
