from pathlib import Path

import pytest

from app.core.errors import ApiError
from app.kernel.md_skill import MdSkillInstructionLoader
from app.kernel.ports.skill_executor import ExecutionResult
from app.kernel.skill_gateway import SkillGateway


class FlakyExecutor:
    tool_type = "TEST"

    def __init__(self):
        self.calls = 0

    def execute(self, config, arguments, timeout_seconds):
        self.calls += 1
        if self.calls < 3:
            return ExecutionResult(status="FAILED", error_code="AI_SKILL_FAILED", error_message="temporary failure")
        return ExecutionResult(status="SUCCEEDED", data={"ok": True})


@pytest.mark.asyncio
async def test_gateway_retries_transient_failure_with_bound():
    gateway = SkillGateway()
    executor = FlakyExecutor()
    result = await gateway._execute_with_retry(
        executor,
        {},
        {},
        timeout_seconds=1,
        max_retries=2,
        backoff_seconds=0,
    )
    assert result.status == "SUCCEEDED"
    assert executor.calls == 3


@pytest.mark.asyncio
async def test_gateway_does_not_retry_validation_failure():
    gateway = SkillGateway()
    executor = FlakyExecutor()
    executor.execute = lambda config, arguments, timeout_seconds: ExecutionResult(
        status="FAILED", error_code="AI_INPUT_INVALID", error_message="invalid"
    )
    result = await gateway._execute_with_retry(
        executor,
        {},
        {},
        timeout_seconds=1,
        max_retries=3,
        backoff_seconds=0,
    )
    assert result.error_code == "AI_INPUT_INVALID"
    assert executor.calls == 0


def test_md_skill_loader_validates_front_matter_and_path(tmp_path: Path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: test skill\n---\n\n# Instructions\nUse approved tools only.\n",
        encoding="utf-8",
    )
    loaded = MdSkillInstructionLoader(tmp_path).load("skill/SKILL.md")
    assert loaded["metadata"]["name"] == "demo"
    assert "approved tools" in loaded["instructions"]

    with pytest.raises(ApiError) as error:
        MdSkillInstructionLoader(tmp_path).load("../SKILL.md")
    assert error.value.code == "AI_SKILL_PATH_INVALID"


def test_md_skill_loader_rejects_missing_metadata(tmp_path: Path):
    (tmp_path / "SKILL.md").write_text("---\nname: missing-description\n---\nbody", encoding="utf-8")
    with pytest.raises(ApiError) as error:
        MdSkillInstructionLoader(tmp_path).load("SKILL.md")
    assert error.value.code == "AI_SKILL_METADATA_INVALID"
