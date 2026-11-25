from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from app.api.routes.a2a import _a2a_task, _task_state
from app.kernel.task.states import TaskStatus


def _task(status):
    return SimpleNamespace(
        id=uuid4(),
        conversation_id=None,
        root_task_id=uuid4(),
        status=status,
        updated_at=datetime.now(timezone.utc),
        result=None,
        error_code=None,
        error_message=None,
    )


def test_a2a_task_state_mapping_covers_task_lifecycle():
    expected = {
        TaskStatus.PENDING: "submitted",
        TaskStatus.QUEUED: "submitted",
        TaskStatus.RUNNING: "working",
        TaskStatus.WAITING_CHILD: "working",
        TaskStatus.WAITING_HUMAN: "input-required",
        TaskStatus.SUCCESS: "completed",
        TaskStatus.CANCELLED: "canceled",
        TaskStatus.FAILED: "failed",
        TaskStatus.TIMEOUT: "failed",
    }
    for status, state in expected.items():
        task = _task(status)
        assert _task_state(task) == state
        assert _a2a_task(task)["status"]["state"] == state


def test_a2a_task_serializes_result_artifact_and_failure_message():
    task = _task(TaskStatus.SUCCESS)
    task.result = {"answer": "ok"}
    serialized = _a2a_task(task)
    assert serialized["artifacts"][0]["parts"][0]["data"] == {"answer": "ok"}

    failed = _task(TaskStatus.FAILED)
    failed.error_code = "AI_TEST_FAILED"
    failed.error_message = "failed safely"
    assert _a2a_task(failed)["status"]["message"]["parts"][0]["text"] == "failed safely"
