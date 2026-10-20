"""思考模型 reasoning_content 链路测试：网关捕获、payload 回传、历史读回。

背景：deepseek-flash 等思考模型多轮回传时，上游要求 assistant 历史消息必须携带
reasoning_content，否则 400 "The reasoning_content in the thinking mode must be
passed back to the API"。
"""

from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.builtin_plugins.model_gateway import OpenAICompatibleGateway
from app.db.session import SessionLocal
from app.kernel.model_gateway.contracts import ModelMessage, ModelRequest
from app.models import (
    Agent,
    AgentVersion,
    AuditLog,
    AuthSession,
    Confirmation,
    Conversation,
    File,
    Message,
    Outbox,
    ShortTermMemory,
    SkillExecutionLog,
    Task,
    TaskCheckpoint,
    TaskEvent,
    Tenant,
    User,
)
from app.security.passwords import hash_password
from app.services.context_builder import assistant_reasoning_parts, build_context


@pytest.fixture(autouse=True)
def cleanup():
    yield
    with SessionLocal.begin() as db:
        # 全量前置清理：上游测试文件残留（审计/记忆/文件等引用 zr_user）会阻塞
        # DELETE FROM zr_user，故按子表→父表顺序清空任务/审计/记忆相关行
        for model in (
            Message,
            Conversation,
            SkillExecutionLog,
            TaskCheckpoint,
            TaskEvent,
            Outbox,
            Task,
            AuditLog,
            ShortTermMemory,
            Confirmation,
            File,
            AgentVersion,
            Agent,
            AuthSession,
            User,
            Tenant,
        ):
            db.execute(delete(model))


def test_normalize_captures_reasoning_content():
    body = {"choices": [{"message": {"content": "答案", "reasoning_content": "思考过程"}, "finish_reason": "stop"}], "usage": {}}
    response = OpenAICompatibleGateway._normalize(body)
    assert response.content == "答案"
    assert response.reasoning_content == "思考过程"


def test_normalize_without_reasoning_defaults_to_none():
    body = {"choices": [{"message": {"content": "答案"}, "finish_reason": "stop"}], "usage": {}}
    assert OpenAICompatibleGateway._normalize(body).reasoning_content is None


def test_message_payload_passes_reasoning_back():
    gateway_payload = OpenAICompatibleGateway._message(ModelMessage("assistant", "答案", reasoning_content="思考"))
    assert gateway_payload["reasoning_content"] == "思考"
    plain = OpenAICompatibleGateway._message(ModelMessage("assistant", "答案"))
    assert "reasoning_content" not in plain


def test_assistant_reasoning_parts_shape():
    assert assistant_reasoning_parts(None) is None
    assert assistant_reasoning_parts("") is None
    assert assistant_reasoning_parts("思考") == [{"type": "reasoning", "text": "思考"}]


def _request(model: str, messages: list[ModelMessage]):
    from app.kernel.model_gateway.contracts import ModelRequest

    return ModelRequest(trace_id="t", conversation_id=uuid4(), messages=messages, model=model)


def test_thinking_model_backfills_empty_reasoning():
    """思考模型多轮：历史 assistant 缺 reasoning 时网关补空串占位（上游 400 根因）。"""
    gateway = OpenAICompatibleGateway("https://api.deepseek.com", "k")
    request = _request("deepseek-flash", [
        ModelMessage("system", "sys"),
        ModelMessage("assistant", "旧回复"),  # 修复前历史，无 reasoning 存档
        ModelMessage("assistant", "新回复", reasoning_content="真实思考"),
        ModelMessage("user", "继续"),
    ])
    payload = gateway._thinking_ready_payload(request)
    assistant_items = [m for m in payload["messages"] if m["role"] == "assistant"]
    assert assistant_items[0]["reasoning_content"] == ""  # 占位补齐
    assert assistant_items[1]["reasoning_content"] == "真实思考"  # 真实值不覆盖


def test_non_thinking_model_not_backfilled():
    gateway = OpenAICompatibleGateway("https://api.deepseek.com", "k")
    request = _request("deepseek-chat", [ModelMessage("assistant", "回复")])
    payload = gateway._thinking_ready_payload(request)
    assert "reasoning_content" not in payload["messages"][0]


def test_build_context_reads_reasoning_back():
    with SessionLocal.begin() as db:
        tenant = Tenant(name=f"reason-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        agent = Agent(tenant_id=tenant.id, owner_user_id=user.id, name="A", source_type="USER", status="ACTIVE")
        db.add(agent)
        db.flush()
        version = AgentVersion(tenant_id=tenant.id, agent_id=agent.id, version_number=1, status="PUBLISHED", config_schema_version="1", config_snapshot={})
        db.add(version)
        db.flush()
        agent.current_published_version_id = version.id
        conversation = Conversation(tenant_id=tenant.id, owner_user_id=user.id, agent_id=agent.id, status="ACTIVE")
        db.add(conversation)
        db.flush()
        db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender_type="USER", content="问题", status="COMPLETED"))
        db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, sender_type="ASSISTANT", content="回答", content_parts=[{"type": "reasoning", "text": "上一轮思考"}], status="COMPLETED"))
        db.flush()

        context = build_context(db, conversation)
        assistant = [m for m in context if m.role == "assistant"][0]
        assert assistant.content == "回答"
        assert assistant.reasoning_content == "上一轮思考"


def test_stream_with_tools_uses_non_stream_decision_round():
    """带 tools 的流式决策轮与任务链路同构（非流式 generate）：
    实测上游「思考模型+tools+流式」不返回 tool_calls，且产品定义要求模型可靠判断是否建 task。"""
    import asyncio
    import json as _json

    import httpx

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json={
            "id": "chatcmpl-1",
            "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "get_time", "arguments": "{}"}},
            ]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = OpenAICompatibleGateway("https://api.deepseek.com", "k", client=client)
    request = ModelRequest(
        trace_id="t", conversation_id=uuid4(),
        messages=[ModelMessage("user", "现在几点")],
        model="deepseek-flash",
        tools=[{"type": "function", "function": {"name": "get_time", "parameters": {}}}],
    )

    async def collect():
        out = []
        async for chunk in gateway.stream_response(request):
            out.append(chunk)
        return out

    chunks = asyncio.run(collect())
    final = [c for c in chunks if c.get("type") == "final"][0]["response"]
    assert final.tool_calls and final.tool_calls[0].name == "get_time"
    assert captured["body"]["stream"] is False  # 决策轮为非流式（与任务链路一致）
    # 决策轮不模拟 delta：文本回复由前端 startTypewriter 播放
    assert all(c.get("type") != "delta" for c in chunks)


def test_stream_without_tools_keeps_real_streaming():
    """无 tools 的纯闲聊：不经过决策轮，仍走真流式。"""
    import asyncio
    import json as _json

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        assert body["stream"] is True
        assert "tools" not in body
        sse = (
            'data: {"choices":[{"delta":{"content":"你好"},"finish_reason":null}]}\n\n'
            'data: [DONE]\n\n'
        )
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = OpenAICompatibleGateway("https://api.deepseek.com", "k", client=client)
    request = ModelRequest(
        trace_id="t", conversation_id=uuid4(),
        messages=[ModelMessage("user", "你好")],
        model="deepseek-chat",
    )

    async def collect():
        out = []
        async for chunk in gateway.stream_response(request):
            out.append(chunk)
        return out

    chunks = asyncio.run(collect())
    assert [c["content"] for c in chunks if c["type"] == "delta"] == ["你好"]
    final = [c for c in chunks if c["type"] == "final"][0]["response"]
    assert final.content == "你好" and not final.tool_calls


def test_guarded_gateway_stream_response_delivers_tool_calls():
    """守卫包装器必须覆写 stream_response：此前基类默认实现只包装文本 stream()，
    结构性丢弃 tool_calls / usage（流式永不建 task 的真正根因）。"""
    import asyncio

    import httpx

    from app.kernel.plugin_guard import GuardedModelGateway

    class _FakeGuard:
        def __init__(self):
            self.records: list[bool] = []
        def check(self, key): pass
        def record(self, key, succeeded): self.records.append(succeeded)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "chatcmpl-1",
            "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "get_time", "arguments": "{}"}},
            ]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    inner = OpenAICompatibleGateway("https://api.deepseek.com", "k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    guard = _FakeGuard()
    guarded = GuardedModelGateway(inner, guard)
    request = ModelRequest(
        trace_id="t", conversation_id=uuid4(),
        messages=[ModelMessage("user", "现在几点")],
        model="deepseek-flash",
        tools=[{"type": "function", "function": {"name": "get_time", "parameters": {}}}],
    )

    async def collect():
        out = []
        async for chunk in guarded.stream_response(request):
            out.append(chunk)
        return out

    chunks = asyncio.run(collect())
    final = [c for c in chunks if c["type"] == "final"][0]["response"]
    assert final.tool_calls and final.tool_calls[0].name == "get_time"
    assert final.usage.total_tokens == 15  # 真实 usage（基类默认实现恒为 0）
    assert guard.records == [True]  # 决策轮经守卫 generate，成败被计数
