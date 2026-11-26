from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.errors import ApiError
from app.api.routes.confirmations import _request_summary
from app.kernel.skill_confirmation import assert_distinct_approver
from app.kernel.skill_gateway import SkillGateway
from app.kernel.md_skill import parse_skill
from app.kernel.md_skill_runtime import MdSkillRuntime


def test_confirmation_requester_cannot_approve_own_request_even_as_admin():
    user_id = uuid4()
    with pytest.raises(ApiError) as excinfo:
        assert_distinct_approver(user_id, user_id)
    assert excinfo.value.code == "AI_CONFIRMATION_SELF_APPROVAL"
    assert excinfo.value.status_code == 403


def test_confirmation_can_be_approved_by_a_different_user():
    assert_distinct_approver(uuid4(), uuid4()) is None


def test_script_approval_summary_is_safe_and_does_not_expose_arguments():
    row = SimpleNamespace(
        request_snapshot={
            "execution_kind": "md_script",
            "script_name": "data-analysis",
            "script_file": "scripts/analyze.py",
            "sandbox_mode": "restricted",
            "sandbox_isolation": "process",
            "arguments": {"api_key": "must-not-leak"},
            "absolute_path": "C:/private/workspace",
        }
    )
    summary = _request_summary(row)
    assert summary == {
        "kind": "md_script",
        "title": "Python 脚本执行",
        "script_name": "data-analysis",
        "script_file": "scripts/analyze.py",
        "sandbox_mode": "restricted",
        "sandbox_isolation": "process",
    }
    assert "arguments" not in summary
    assert "absolute_path" not in summary


def test_approved_script_resume_requires_identical_script_and_arguments():
    skill_version_id = uuid4()
    checkpoint = SimpleNamespace(
        pending_call={
            "implementation": "md_script",
            "skill_version_id": str(skill_version_id),
            "script_file": "scripts/main.py",
            "script_fingerprint": "fingerprint-v1",
            "arguments": {"count": 3},
        }
    )
    manifest = SimpleNamespace(
        script_files=["scripts/main.py"],
        file_fingerprint="fingerprint-v1",
    )
    assert SkillGateway._check_approved_script(
        checkpoint,
        skill_version_id=skill_version_id,
        manifest=manifest,
        arguments={"count": 3},
    ) is None

    with pytest.raises(ApiError) as excinfo:
        SkillGateway._check_approved_script(
            checkpoint,
            skill_version_id=skill_version_id,
            manifest=manifest,
            arguments={"count": 4},
        )
    assert excinfo.value.code == "AI_SANDBOX_DENIED"


@pytest.mark.asyncio
async def test_runtime_rejects_modified_files_after_skill_version_was_published(tmp_path):
    skill_root = tmp_path / "demo"
    scripts = skill_root / "scripts"
    scripts.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        """---
name: demo
description: demo script
implementation: md_script
version: 1
input_schema:
  type: object
  properties: {}
output_schema:
  type: object
allow_scripts: true
required_permissions: []
windows_trusted: true
---
Run the demo script.
""",
        encoding="utf-8",
    )
    script = scripts / "main.py"
    script.write_text('print("{}")\n', encoding="utf-8")
    published_manifest = parse_skill(tmp_path, "demo")

    # 发布后有人直接替换了脚本文件；旧 SkillVersion 快照不得执行新内容。
    script.write_text('print("{\\"changed\\": true}")\n', encoding="utf-8")
    runtime = MdSkillRuntime(
        tmp_path,
        enabled=True,
        sandbox_driver=object(),
        windows_process_enabled=True,
        allowed_roots=[str(tmp_path)],
    )
    with pytest.raises(ApiError) as excinfo:
        await runtime.execute(published_manifest, {})
    assert excinfo.value.code == "AI_SKILL_VERSION_DRIFT"
    assert excinfo.value.status_code == 409
