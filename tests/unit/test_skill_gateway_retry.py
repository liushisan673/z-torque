import asyncio

from app.kernel.ports.skill_executor import ExecutionResult
from app.kernel.skill_gateway import SkillGateway


class _FailingExecutor:
    tool_type = "HTTP"

    def __init__(self):
        self.calls = 0

    def execute(self, config, arguments, timeout_seconds):
        self.calls += 1
        return ExecutionResult(
            status="FAILED",
            error_code="AI_HTTP_REQUEST_FAILED",
            error_message="upstream timed out",
        )


def test_http_post_is_not_retried_by_default_after_ambiguous_failure():
    gateway = SkillGateway()
    executor = _FailingExecutor()
    result = asyncio.run(
        gateway._execute_with_retry(
            executor,
            {"method": "POST"},
            {"amount": 100},
            timeout_seconds=1,
            max_retries=3,
            backoff_seconds=0,
        )
    )
    assert result.status == "FAILED"
    assert executor.calls == 1


def test_http_get_remains_retryable():
    gateway = SkillGateway()
    executor = _FailingExecutor()
    result = asyncio.run(
        gateway._execute_with_retry(
            executor,
            {"method": "GET"},
            {"query": "status"},
            timeout_seconds=1,
            max_retries=2,
            backoff_seconds=0,
        )
    )
    assert result.status == "FAILED"
    assert executor.calls == 3


def test_http_post_can_opt_in_to_retry_only_when_declared_idempotent():
    gateway = SkillGateway()
    executor = _FailingExecutor()
    result = asyncio.run(
        gateway._execute_with_retry(
            executor,
            {"method": "POST", "idempotent": True},
            {"query": "safe-upsert"},
            timeout_seconds=1,
            max_retries=2,
            backoff_seconds=0,
        )
    )
    assert result.status == "FAILED"
    assert executor.calls == 3
