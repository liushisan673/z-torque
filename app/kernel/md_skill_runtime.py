from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

from app.core.errors import ApiError
from app.kernel.md_skill import MdSkillManifest, validate_input
from app.kernel.ports.sandbox_driver import SandboxDriver


class ProcessSandboxDriver(SandboxDriver):
    """sandbox.driver 内核默认实现：进程内受限执行（独立解释器 -I + 最小环境变量）。

    Windows 无可靠的进程级网络/文件系统沙箱，由调用方（MdSkillRuntime）按平台
    显式降级；企业版可经 PortRegistry 以 PLUGIN/EXPLICIT 优先级替换为容器隔离。
    """

    async def run(self, *, script_path: Path, arguments: dict[str, Any], cwd: Path, timeout_seconds: float, max_output_bytes: int) -> dict[str, Any]:
        return await run_isolated_process(script_path=script_path, arguments=arguments, cwd=cwd, timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes)


async def run_isolated_process(*, script_path: Path, arguments: dict[str, Any], cwd: Path, timeout_seconds: float, max_output_bytes: int) -> dict[str, Any]:
    """受控执行单脚本：stdin 传 JSON 参数、stdout 收 JSON 结果（sandbox.driver 内核实现核心）。"""
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONNOUSERSITE": "1"}
    process = await asyncio.create_subprocess_exec(sys.executable, "-I", str(script_path), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(cwd), env=env)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(arguments).encode()), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise ApiError("AI_SKILL_TIMEOUT", "MD-Skill script timed out", 504)
    if len(stdout) > max_output_bytes or len(stderr) > max_output_bytes:
        raise ApiError("AI_SKILL_FAILED", "MD-Skill script output exceeded limit", 500)
    if process.returncode != 0:
        raise ApiError("AI_SKILL_FAILED", "MD-Skill script failed", 500)
    return json.loads(stdout)


class MdSkillRuntime:
    """Run only registered scripts; capability stays unavailable without isolation."""

    def __init__(self, root: str | Path, *, enabled: bool = False, timeout_seconds: float = 10, max_output_bytes: int = 65536, sandbox_driver: SandboxDriver | None = None) -> None:
        self.root = Path(root).resolve()
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        # sandbox.driver Port 解析结果由装配根经插件注入（P2 收口）；未注入时
        # 使用内核默认进程内受限执行（直构场景语义不变，单一路径无分支）
        self._sandbox = sandbox_driver or ProcessSandboxDriver()

    async def execute(self, manifest: MdSkillManifest, arguments: dict[str, Any]) -> dict[str, Any]:
        validate_input(manifest, arguments)
        if manifest.implementation != "md_script" or not manifest.allow_scripts:
            raise ApiError("AI_SKILL_SCRIPT_DISABLED", "script execution is not enabled for this skill", 503)
        if not self.enabled:
            raise ApiError("AI_CAPABILITY_UNAVAILABLE", "MD-Skill script runtime is disabled", 503)
        # Windows has no reliable process-level network/filesystem sandbox here.
        if os.name == "nt":
            raise ApiError("AI_CAPABILITY_UNAVAILABLE", "MD-Skill script isolation is unavailable on this platform", 503)
        if len(manifest.script_files) != 1:
            raise ApiError("AI_SKILL_INVALID_MANIFEST", "exactly one script is required", 422)
        script = (self.root / manifest.relative_path / manifest.script_files[0]).resolve()
        skill_root = (self.root / manifest.relative_path).resolve()
        if skill_root not in script.parents or script.suffix != ".py" or not script.is_file():
            raise ApiError("AI_SKILL_PATH_INVALID", "script path is invalid", 422)
        result = await self._sandbox.run(script_path=script, arguments=arguments, cwd=skill_root, timeout_seconds=self.timeout_seconds, max_output_bytes=self.max_output_bytes)
        try:
            validate(result, manifest.output_schema)
        except ValidationError as exc:
            raise ApiError("AI_SKILL_FAILED", "MD-Skill output does not match schema", 500) from exc
        return result
