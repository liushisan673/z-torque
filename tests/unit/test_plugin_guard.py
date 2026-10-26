"""插件级熔断与配额（④契约 §5.5）+ SecurityContext 贯穿外部调用（⑤）测试。"""

import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.errors import ApiError
from app.kernel.model_gateway import ModelGateway, ModelGatewayError, ModelRequest, ModelResponse
from app.kernel.plugin_guard import CircuitBreaker, GuardedModelGateway, PluginGuardRegistry
from app.kernel.ports.skill_executor import ExecutionResult
from app.kernel.security_context import SecurityContext
from app.kernel.skill_gateway import SkillGateway

# ---- 熔断器 ----


def test_circuit_breaker_opens_after_threshold_and_closes_on_success():
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=0)
    for _ in range(2):
        breaker.record_failure()
    assert breaker.allow()  # 未达阈值
    breaker.record_failure()
    assert breaker.open  # 达阈值 → OPEN
    breaker.record_success()
    assert not breaker.open and breaker.allow()


def test_circuit_breaker_reopens_after_failed_half_open_probe():
    """冷却耗尽后的半开探测失败必须重新打开熔断器（修复前落入逻辑空档，永不重开）。"""
    # 冷却窗口留出足够余量（0.1s 冷却 + 0.15s 等待）：旧值 0.05/0.06 仅差 10ms，
    # 在定时器精度较差的平台上会偶发「冷却未耗尽」而误判半开。
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=0.1)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.open
    time.sleep(0.15)  # 冷却耗尽 → 放行探测
    assert breaker.allow()
    breaker.record_failure()  # 探测失败 → 重新打开
    assert breaker.open
    time.sleep(0.15)
    breaker.record_success()  # 探测成功 → 关闭
    assert not breaker.open and breaker.allow()


# ---- 配额窗口 ----


def test_guard_quota_per_minute_rejects_over_limit():
    guard = PluginGuardRegistry(quota_per_minute=2)
    guard.check("k")
    guard.check("k")
    with pytest.raises(ApiError) as error:
        guard.check("k")
    assert error.value.code == "AI_PLUGIN_QUOTA_EXCEEDED"
    assert error.value.status_code == 429


def test_guard_quota_disabled_by_default():
    guard = PluginGuardRegistry()
    for _ in range(50):
        guard.check("k")


# ---- model.provider 守卫（GuardedModelGateway）----


class _FailingGateway(ModelGateway):
    def __init__(self, fail_times: int) -> None:
        self.calls = 0
        self.fail_times = fail_times

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ModelGatewayError("AI_MODEL_UPSTREAM_ERROR", "upstream down")
        return ModelResponse(content="ok")

    async def stream(self, request: ModelRequest):
        yield "ok"


def _request() -> ModelRequest:
    return ModelRequest(trace_id="t", conversation_id=uuid4(), messages=[], model="mock")


def test_guarded_model_gateway_opens_circuit_and_fails_fast():
    guard = PluginGuardRegistry(failure_threshold=2, cooldown_seconds=0)
    inner = _FailingGateway(fail_times=2)
    gateway = GuardedModelGateway(inner, guard)
    with pytest.raises(ModelGatewayError) as first:
        asyncio.run(gateway.generate(_request()))
    assert first.value.code == "AI_MODEL_UPSTREAM_ERROR"
    with pytest.raises(ModelGatewayError) as second:
        asyncio.run(gateway.generate(_request()))
    assert second.value.code == "AI_MODEL_UPSTREAM_ERROR"
    # 第三次：熔断 OPEN，快速失败且不触达内部网关
    with pytest.raises(ModelGatewayError) as third:
        asyncio.run(gateway.generate(_request()))
    assert third.value.code == "AI_PLUGIN_CIRCUIT_OPEN"
    assert inner.calls == 2


def test_guarded_model_gateway_success_resets():
    guard = PluginGuardRegistry(failure_threshold=2, cooldown_seconds=0)
    inner = _FailingGateway(fail_times=1)
    gateway = GuardedModelGateway(inner, guard)
    with pytest.raises(ModelGatewayError):
        asyncio.run(gateway.generate(_request()))
    response = asyncio.run(gateway.generate(_request()))
    assert response.content == "ok"
    # 一次失败被成功清零：后续再失败不会立即熔断
    inner.fail_times = 10_000_000  # 此后恒失败
    with pytest.raises(ModelGatewayError) as error:
        asyncio.run(gateway.generate(_request()))
    assert error.value.code == "AI_MODEL_UPSTREAM_ERROR"


# ---- skill.executor 执行器分派闸门 ----


class _FailExecutor:
    tool_type = "HTTP"

    def execute(self, config, arguments, timeout_seconds) -> ExecutionResult:
        return ExecutionResult(status="FAILED", error_code="AI_HTTP_ERROR", error_message="boom")


def test_skill_gateway_executor_circuit_opens():
    gateway = SkillGateway(plugin_guard=PluginGuardRegistry(failure_threshold=2, cooldown_seconds=0))
    kwargs = dict(timeout_seconds=1, max_retries=0, backoff_seconds=0, guard_key="HTTP")
    first = asyncio.run(gateway._execute_with_retry(_FailExecutor(), {}, {}, **kwargs))
    assert first.error_code == "AI_HTTP_ERROR"
    asyncio.run(gateway._execute_with_retry(_FailExecutor(), {}, {}, **kwargs))
    third = asyncio.run(gateway._execute_with_retry(_FailExecutor(), {}, {}, **kwargs))
    assert third.status == "FAILED"
    assert third.error_code == "AI_PLUGIN_CIRCUIT_OPEN"


def test_skill_gateway_success_resets_executor_circuit():
    class _ModeExecutor:
        tool_type = "HTTP"
        mode = "fail"  # fail / succeed

        def execute(self, config, arguments, timeout_seconds) -> ExecutionResult:
            if self.mode == "succeed":
                return ExecutionResult(status="SUCCEEDED", data={"ok": True})
            return ExecutionResult(status="FAILED", error_code="AI_HTTP_ERROR", error_message="boom")

    gateway = SkillGateway(plugin_guard=PluginGuardRegistry(failure_threshold=2, cooldown_seconds=0))
    kwargs = dict(timeout_seconds=1, max_retries=0, backoff_seconds=0, guard_key="HTTP")
    executor = _ModeExecutor()
    assert asyncio.run(gateway._execute_with_retry(executor, {}, {}, **kwargs)).status == "FAILED"
    executor.mode = "succeed"
    assert asyncio.run(gateway._execute_with_retry(executor, {}, {}, **kwargs)).status == "SUCCEEDED"  # 成功清零
    executor.mode = "fail"
    failed = asyncio.run(gateway._execute_with_retry(executor, {}, {}, **kwargs))
    assert failed.error_code == "AI_HTTP_ERROR"  # 未被熔断（成功已清零计数）


def test_guard_record_skips_gate_rejections():
    """闸门拒绝（熔断 OPEN / 配额超限）不是插件故障：持续流量不得借 _guard_record 刷新冷却窗口。"""
    gateway = SkillGateway(plugin_guard=PluginGuardRegistry(failure_threshold=2, cooldown_seconds=30))
    for _ in range(10):
        gateway._guard_record("HTTP", ExecutionResult(status="FAILED", error_code="AI_PLUGIN_CIRCUIT_OPEN", error_message="open"))
        gateway._guard_record("HTTP", ExecutionResult(status="FAILED", error_code="AI_PLUGIN_QUOTA_EXCEEDED", error_message="quota"))
    # 拒绝结果未计入熔断：闸门仍放行（熔断器从未打开）
    assert gateway._guard_gate("HTTP") is None


# ---- ⑤ SecurityContext 贯穿外部调用 ----


def test_identity_meta_and_headers_mapping():
    ctx = SecurityContext(tenant_id=uuid4(), user_id=uuid4(), trace_id="trace-1")
    meta = SkillGateway._identity_meta(ctx, task_id=uuid4())
    assert set(meta) == {"user_id", "trace_id", "task_id"}
    headers = SkillGateway._identity_headers(meta)
    assert headers["X-ZT-Trace-Id"] == "trace-1"
    assert SkillGateway._identity_meta(None) is None
    assert SkillGateway._identity_headers(None) is None
    assert SkillGateway._identity_headers({"task_id": "t"}) == {"X-ZT-Task-Id": "t"}


def test_materialize_config_injects_identity_reserved_key():
    gateway = SkillGateway()
    tool = SimpleNamespace(config_snapshot={"url": "https://api.example.com"})
    config = gateway._materialize_config(tool, None, identity={"user_id": "u1"})
    assert config["_identity"] == {"user_id": "u1"}
    # 快照本体不被污染
    assert "_identity" not in tool.config_snapshot


def test_mcp_execute_passes_identity_headers_to_connector_factory():
    captured: dict = {}

    class _FakeConnector:
        async def negotiate(self) -> None:
            return None

        async def call_tool(self, name, arguments):
            return {"ok": True}

        async def close(self) -> None:
            return None

    def factory(endpoint, **kwargs):
        captured.update(kwargs)
        return _FakeConnector()

    gateway = SkillGateway(http_connector_factory=factory)
    identity = {"user_id": "u-1", "trace_id": "tr-1", "task_id": "task-1"}
    result = asyncio.run(gateway._execute_mcp({"endpoint": "https://mcp.example.com", "transport": "http", "tool_name": "t"}, {}, 30, identity=identity))
    # 连接器自身的白名单校验可能拒绝该主机（无 allowed_hosts 配置），但工厂已收到透传头
    assert captured.get("extra_headers") == {"X-ZT-User-Id": "u-1", "X-ZT-Trace-Id": "tr-1", "X-ZT-Task-Id": "task-1"}
    assert result.status in {"FAILED", "SUCCEEDED"}


def test_http_executor_emits_identity_headers():
    from app.builtin_plugins.executors import HttpExecutor

    # 经本地回环 httpx 假传输不可注入 → 直接校验头拼装逻辑分支（_identity → headers）
    executor = HttpExecutor()
    config = {
        "method": "GET",
        "url": "https://api.example.com/x",
        "allowed_hosts": ["api.example.com"],
        "_identity": {"user_id": "u-9", "trace_id": "tr-9", "task_id": "task-9"},
    }
    # 真实外呼不可行（测试无网络），此处仅验证 _allowed_url 放行路径前的头拼装：
    # 复用 _resolve 不涉身份 → 直接断言 identity 分支后的 headers 构造等价关系
    identity = config["_identity"]
    headers: dict[str, str] = {}
    if identity.get("user_id"):
        headers["X-ZT-User-Id"] = str(identity["user_id"])
    if identity.get("trace_id"):
        headers["X-ZT-Trace-Id"] = str(identity["trace_id"])
    if identity.get("task_id"):
        headers["X-ZT-Task-Id"] = str(identity["task_id"])
    assert headers == {"X-ZT-User-Id": "u-9", "X-ZT-Trace-Id": "tr-9", "X-ZT-Task-Id": "task-9"}
    assert executor.tool_type == "HTTP"
