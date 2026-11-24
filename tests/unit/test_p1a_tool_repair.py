import asyncio
from uuid import uuid4

from app.kernel.model_gateway import ModelMessage, ModelRequest, ModelResponse, ModelToolCall
from app.kernel.ports.skill_executor import ExecutionResult
from app.kernel.security_context import SecurityContext
from app.services import execution_loop
from app.services.function_calling import ToolMapping, validate_tool_calls


def test_validate_tool_calls_reports_json_path_and_did_you_mean():
    mapping = ToolMapping(
        "calc", uuid4(), uuid4(),
        {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]},
    )
    result = validate_tool_calls([ModelToolCall("c1", "calc", {"count": "3"})], {"calc": mapping})
    assert result[0].error is not None
    assert result[0].error["json_path"] == "$.count"
    assert result[0].error["did_you_mean"] == "3"
    assert "唯一一次结构化修复回合" in result[0].error["self_correction"]


def test_execution_loop_repairs_arguments_once(monkeypatch):
    captured: list = []
    audits: list[str] = []
    monkeypatch.setattr(execution_loop, "_progress_event", lambda *args, **kwargs: None)

    def fake_audit(*args, **kwargs):
        audits.append(kwargs["action"])

    monkeypatch.setattr(execution_loop, "record_audit", fake_audit)

    mapping = ToolMapping(
        "calc", uuid4(), uuid4(),
        {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]},
    )

    class FakeGateway:
        def __init__(self):
            self.calls = 0

        async def generate(self, request):
            self.calls += 1
            captured.append(request.messages)
            if self.calls == 1:
                return ModelResponse(tool_calls=[ModelToolCall("tc1", "calc", {"count": "3"})])
            if self.calls == 2:
                return ModelResponse(tool_calls=[ModelToolCall("tc1", "calc", {"count": 3})])
            return ModelResponse(content="done")

    class FakeSkillGateway:
        def __init__(self):
            self.calls = 0

        async def execute_async(self, *args, **kwargs):
            self.calls += 1
            return ExecutionResult(status="SUCCEEDED", data={"value": 3})

    class FakeDB:
        def get(self, model, key):
            return None

        def scalar(self, *args, **kwargs):
            return None

    gateway = FakeGateway()
    skill = FakeSkillGateway()
    loop = execution_loop.AgentExecutionLoop(gateway, skill)  # type: ignore[arg-type]
    request = ModelRequest(trace_id="t", messages=[ModelMessage("user", "calc")], model="m")
    result = asyncio.run(loop.run(request=request, mappings={"calc": mapping}, db=FakeDB(), ctx=SecurityContext(tenant_id=uuid4(), user_id=uuid4())))
    assert result.response.content == "done"
    assert gateway.calls == 3
    assert skill.calls == 1
    first_repair_message = next(m for m in captured[1] if m.tool_call_id == "tc1")
    assert "INVALID_ARGUMENTS" in first_repair_message.content
    assert "$.count" in first_repair_message.content
    assert audits == ["TOOL_ARGUMENT_INVALID", "TOOL_CALL_REPAIRED"]
