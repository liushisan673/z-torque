"""Phase 2 阶段三：Windows 能力显式降级与文档化验收。

锁定三件事：capabilities 不变量自洽、Windows 可信闸门真实拒绝、约束有正式文档。
不新增能力、不放宽任何闸门。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.errors import (
    PLUGIN_ERROR_CODES,
    PLUGIN_ERROR_STATUS,
    SKILL_WINDOWS_UNTRUSTED,
    ApiError,
)
from app.kernel.md_skill import MdSkillManifest
from app.kernel.md_skill_runtime import MdSkillRuntime
from app.main import app

_ROOT = Path(__file__).resolve().parents[2]
_DOC = _ROOT / "docs" / "Z-Torque_Windows_能力降级与沙箱边界_v1.md"
_RUNTIME_SOURCE = _ROOT / "app" / "kernel" / "md_skill_runtime.py"


def _manifest(*, windows_trusted: bool = False) -> MdSkillManifest:
    return MdSkillManifest(
        name="demo",
        description="",
        implementation="md_script",
        declared_version=1,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        allow_scripts=True,
        required_permissions=[],
        instructions="",
        script_files=["main.py"],
        file_fingerprint="x",
        relative_path="demo",
        windows_trusted=windows_trusted,
    )


@pytest.mark.asyncio
async def test_capabilities_sandbox_is_honest_and_self_consistent():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/z-torque/capabilities")
    assert response.status_code == 200, response.text
    sandbox = response.json()["data"]["sandbox"]
    assert sandbox["scope"]["explicit"] is True
    assert sandbox["scope"]["full_requires_container"] is True
    assert sandbox["scope"]["gateway_only"] is True
    assert sandbox["script"]["windows_requires_trusted"] is True
    assert sandbox["script"]["windows_requires_confirmation"] is True
    level = sandbox["isolation_level"]
    untrusted = sandbox["untrusted_execution"]
    assert (untrusted == "container") == (level == "container"), "无容器时不得上报不可信执行可用"


@pytest.mark.asyncio
async def test_capabilities_sandbox_keeps_structure_when_unassembled(monkeypatch):
    monkeypatch.setattr(app.state, "assembly_plugins", ())
    monkeypatch.setattr(app.state, "port_registry", None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/z-torque/capabilities")
    sandbox = response.json()["data"]["sandbox"]
    assert sandbox["available"] is False
    assert sandbox["untrusted_execution"] == "unavailable"
    assert sandbox["scope"]["full_requires_container"] is True
    assert sandbox["script"]["windows_requires_trusted"] is True
    assert sandbox["script"]["windows_requires_confirmation"] is True


@pytest.mark.asyncio
async def test_windows_untrusted_skill_is_rejected(monkeypatch):
    import app.kernel.md_skill_runtime as runtime_module

    monkeypatch.setattr(runtime_module, "os", SimpleNamespace(name="nt"))
    runtime = MdSkillRuntime(root=_ROOT, enabled=True, windows_process_enabled=True)
    with pytest.raises(ApiError) as exc:
        await runtime.execute(_manifest(windows_trusted=False), {}, approval_granted=True)
    assert exc.value.code == SKILL_WINDOWS_UNTRUSTED
    assert exc.value.status_code == 403


def test_windows_gate_error_code_is_registered():
    assert SKILL_WINDOWS_UNTRUSTED in PLUGIN_ERROR_CODES
    assert PLUGIN_ERROR_STATUS[SKILL_WINDOWS_UNTRUSTED] == 403


def test_windows_downgrade_is_formally_documented():
    text = _DOC.read_text(encoding="utf-8")
    for token in (
        "AI_CAPABILITY_UNAVAILABLE",
        "AI_SKILL_WINDOWS_UNTRUSTED",
        "untrusted_execution",
        "full_requires_container",
        "windows_requires_trusted",
        "windows_requires_confirmation",
        "fail-closed",
    ):
        assert token in text, f"missing documented constraint: {token}"


def test_gate_checks_exist_in_runtime_source():
    source = _RUNTIME_SOURCE.read_text(encoding="utf-8")
    assert "windows_process_enabled" in source
    assert "windows_trusted" in source
    assert "AI_SANDBOX_DENIED" in source
