"""插件级熔断与配额（契约 §5.5：单个插件不得拖垮内核）。

熔断：按插件内组件键（model.provider / executor.<TOOL_TYPE>）跟踪连续失败，
超过阈值进入 OPEN（后续调用快速失败 AI_PLUGIN_CIRCUIT_OPEN），冷却后放行探测，
探测成功关闭、失败重新打开。
配额：60s 滑动窗口调用计数，超限快速失败 AI_PLUGIN_QUOTA_EXCEEDED（0=不限）。

接线缝（社区版默认启用）：
- model.provider：GuardedModelGateway 装饰内置网关（ModelProviderPlugin）；
- skill.executor：执行器分派前闸门 + 结果回收（SkillGateway，FAILED 计数）。
"""

import time
from collections import deque
from typing import Any, AsyncIterator

from app.core.errors import ApiError
from app.kernel.model_gateway import ModelGateway, ModelGatewayError, ModelRequest, ModelResponse

CIRCUIT_OPEN_CODE = "AI_PLUGIN_CIRCUIT_OPEN"
QUOTA_EXCEEDED_CODE = "AI_PLUGIN_QUOTA_EXCEEDED"

_DEFAULT_FAILURE_THRESHOLD = 8
_DEFAULT_COOLDOWN_SECONDS = 30.0
_DEFAULT_QUOTA_PER_MINUTE = 0  # 0 = 不限


class CircuitBreaker:
    """连续失败熔断器：threshold 次连续失败 → OPEN；冷却后放行探测（半开）。"""

    def __init__(self, failure_threshold: int, cooldown_seconds: float) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def open(self) -> bool:
        if self._opened_at is None:
            return False
        # cooldown<=0 语义：闩锁式熔断（保持 OPEN 直至成功复位）
        cooldown = self.cooldown_seconds if self.cooldown_seconds > 0 else float("inf")
        return (time.monotonic() - self._opened_at) < cooldown

    def allow(self) -> bool:
        return not self.open

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._opened_at is None:
            if self._consecutive_failures >= self.failure_threshold:
                self._opened_at = time.monotonic()
        else:
            # 已处于打开/半开态：任何失败都重新计时（半开探测失败 → 重新打开）。
            # 此前实现仅在仍处于冷却窗口内刷新；冷却耗尽后的探测失败会落入两个
            # 分支都不命中（open 已为 False 且 _opened_at 非 None），熔断器从此
            # 永不重开——后续无论失败多少次都保持放行。
            self._opened_at = time.monotonic()

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None


class _QuotaWindow:
    def __init__(self, limit_per_minute: int) -> None:
        self.limit = limit_per_minute
        self._hits: deque[float] = deque()

    def reject(self) -> bool:
        if self.limit <= 0:
            return False
        now = time.monotonic()
        while self._hits and now - self._hits[0] > 60.0:
            self._hits.popleft()
        if len(self._hits) >= self.limit:
            return True
        self._hits.append(now)
        return False


class PluginGuardRegistry:
    """按组件键管理熔断器与配额窗口；check 通过即计一次配额。"""

    def __init__(self, *, failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD, cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS, quota_per_minute: int = _DEFAULT_QUOTA_PER_MINUTE) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._quota_per_minute = quota_per_minute
        self._breakers: dict[str, CircuitBreaker] = {}
        self._quotas: dict[str, _QuotaWindow] = {}

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> "PluginGuardRegistry":
        return cls(
            failure_threshold=getattr(settings, "plugin_breaker_failure_threshold", _DEFAULT_FAILURE_THRESHOLD),
            cooldown_seconds=getattr(settings, "plugin_breaker_cooldown_seconds", _DEFAULT_COOLDOWN_SECONDS),
            quota_per_minute=getattr(settings, "plugin_quota_per_minute", _DEFAULT_QUOTA_PER_MINUTE),
        )

    def check(self, key: str) -> None:
        """闸门：熔断 OPEN 或配额超限抛 ApiError（503/429）；通过则记配额。"""
        breaker = self._breakers.get(key)
        if breaker is not None and not breaker.allow():
            raise ApiError(CIRCUIT_OPEN_CODE, f"plugin component {key} circuit is open (cooling down)", 503)
        quota = self._quotas.setdefault(key, _QuotaWindow(self._quota_per_minute))
        if quota.reject():
            raise ApiError(QUOTA_EXCEEDED_CODE, f"plugin component {key} exceeded quota ({self._quota_per_minute}/min)", 429)

    def record(self, key: str, *, succeeded: bool) -> None:
        breaker = self._breakers.setdefault(key, CircuitBreaker(self._failure_threshold, self._cooldown_seconds))
        if succeeded:
            breaker.record_success()
        else:
            breaker.record_failure()


class GuardedModelGateway(ModelGateway):
    """model.provider 熔断/配额守卫（装饰器，语义透传；熔断以 ModelGatewayError 呈现）。"""

    def __init__(self, inner: ModelGateway, guard: PluginGuardRegistry, key: str = "model.provider") -> None:
        self._inner = inner
        self._guard = guard
        self._key = key

    def _gate(self) -> None:
        try:
            self._guard.check(self._key)
        except ApiError as exc:
            raise ModelGatewayError(exc.code, exc.message) from exc

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self._gate()
        try:
            response = await self._inner.generate(request)
        except ModelGatewayError:
            self._guard.record(self._key, succeeded=False)
            raise
        self._guard.record(self._key, succeeded=True)
        return response

    async def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        self._gate()
        try:
            async for delta in self._inner.stream(request):
                yield delta
        except ModelGatewayError:
            self._guard.record(self._key, succeeded=False)
            raise
        self._guard.record(self._key, succeeded=True)

    async def stream_response(self, request: ModelRequest) -> AsyncIterator[dict[str, Any]]:
        """流式决策聚合（守卫语义透传）：**必须覆写基类默认实现**——基类只包装文本 stream()，
        会结构性丢弃 tool_calls / usage（此前流式永不建 task 的根因）。

        - 带 tools：决策轮经守卫 generate（与任务链路同构，计入熔断/配额），只产出 final，
          文本回复由前端 startTypewriter 播放；
        - 无 tools：透传内部网关真流式，整流结束按成败记录守卫计数。
        """
        if request.tools:
            response = await self.generate(request)  # 守卫包裹：check + 成败计数
            yield {'type': 'final', 'response': response}
            return
        self._gate()
        failed = False
        try:
            async for chunk in self._inner.stream_response(request):
                yield chunk
        except ModelGatewayError:
            failed = True
            raise
        finally:
            self._guard.record(self._key, succeeded=not failed)

    def __getattr__(self, item: str) -> Any:
        # 透传内部网关属性（default_model 等），装饰器对外语义与被包装网关一致
        return getattr(self._inner, item)
