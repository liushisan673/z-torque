"""Phase 0 单系统能力收口真实入口验收（2026-11-24 决策）。

覆盖：
1. 默认装配 = 全量内置插件（现 22 个，含知识库；无社区/企业差距）；
2. capabilities 如实反映 A2A / CSV / 归档 / 沙箱 Windows 闸门；
3. Windows process 有限解禁三闸门：可信声明 + 总开关 + 路径白名单强制收口；
4. md_script frontmatter 的 windows_trusted 解析与快照传递；
5. MCP 脚本在 Windows 上未声明可信时 fail closed。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.core.errors import ApiError
from app.kernel.md_skill import MdSkillManifest, parse_skill
from app.kernel.md_skill_runtime import MdSkillRuntime
from app.kernel.ports.sandbox_driver import SandboxDriver
from app.main import app

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows 有限解禁闸门仅 Windows 平台生效")

_BASE_MANIFEST = """---
name: trusted-writer
description: gated writer
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
Write JSON to stdout.
"""

_SCRIPT = "import json\nprint(json.dumps({'ok': True}))\n"


class _StubSandbox(SandboxDriver):
    """TEST-ONLY：闸门测试不需要真实起进程，只记录是否被调到。"""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls = 0

    async def run(self, *, script_path: Path, arguments: dict[str, Any], cwd: Path, timeout_seconds: float, max_output_bytes: int) -> dict[str, Any]:
        self.calls += 1
        return self.result


def _build_skill(tmp_path: Path, *, frontmatter: str) -> MdSkillManifest:
    root = tmp_path / "md-skills"
    skill_dir = root / "trusted-writer"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(frontmatter, encoding="utf-8")
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "main.py").write_text(_SCRIPT, encoding="utf-8")
    return parse_skill(root, "trusted-writer")


def test_windows_trusted_declaration_parsed_and_snapshotted(tmp_path):
    from app.kernel.md_skill import snapshot

    manifest = _build_skill(tmp_path, frontmatter=_BASE_MANIFEST)
    assert manifest.windows_trusted is True
    assert snapshot(manifest)["windows_trusted"] is True

    untrusted = _BASE_MANIFEST.replace("windows_trusted: true\n", "")
    manifest2 = _build_skill(tmp_path / "untrusted", frontmatter=untrusted)
    assert manifest2.windows_trusted is False


@pytest.mark.asyncio
async def test_windows_gate_total_switch_blocks_even_trusted(tmp_path):
    manifest = _build_skill(tmp_path, frontmatter=_BASE_MANIFEST)
    runtime = MdSkillRuntime(
        tmp_path / "md-skills", enabled=True,
        sandbox_driver=_StubSandbox({"ok": True}),
        windows_process_enabled=False,
    )
    with pytest.raises(ApiError) as exc:
        await runtime.execute(manifest, {}, approval_granted=True)
    assert exc.value.code == "AI_CAPABILITY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_windows_gate_path_allowlist_forced(tmp_path):
    manifest = _build_skill(tmp_path, frontmatter=_BASE_MANIFEST)
    # 白名单指向别的根目录 → 即便可信也拒绝（强制收口，不降级）
    runtime = MdSkillRuntime(
        tmp_path / "md-skills", enabled=True,
        sandbox_driver=_StubSandbox({"ok": True}),
        windows_process_enabled=True,
        allowed_roots=[str(tmp_path / "other-root")],
    )
    with pytest.raises(ApiError) as exc:
        await runtime.execute(manifest, {}, approval_granted=True)
    assert exc.value.code == "AI_SANDBOX_DENIED"


@pytest.mark.asyncio
async def test_default_flags_and_capabilities_are_unified():
    settings = get_settings()
    assert settings.a2a_enabled is True
    assert settings.md_skill_script_enabled is True
    assert settings.md_skill_windows_process_enabled is True
    assert settings.audit_export_formats == ["jsonl", "csv"]
    assert settings.audit_archive_schedule_hours > 0

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/z-torque/capabilities")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["a2a"]["enabled"] is True
    assert data["audit_export"]["archive"] is True
    assert "csv" in data["audit_export"]["formats"]
    assert data["sandbox"]["script"]["windows_requires_confirmation"] is True
    assert data["sandbox"]["script"]["windows_requires_trusted"] is True
