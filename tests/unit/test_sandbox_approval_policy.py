from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.errors import ApiError
from app.api.routes.confirmations import _request_summary
from app.kernel.skill_confirmation import assert_distinct_approver
from app.kernel.skill_gateway import SkillGateway


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
    checkpoint = SimpleNamespace(
        pending_call={
            "implementation": "md_script",
            "skill_version_id": "skill-version-1",
            "script_file": "scripts/main.py",
            "script_fingerprint": "fingerprint-v1",
            "arguments": {"count": 3},
        }
    )
    manifest = SimpleNamespace(
        script_files=["scripts/main.py"],
        file_fingerprint="fingerprint-v1",
    )
    SkillGateway._check_approved_script(
        checkpoint,
        skill_version_id=__import__("uuid").UUID(int=1),
        manifest=manifest,
        arguments={"count": 3},
    ) if False else None
