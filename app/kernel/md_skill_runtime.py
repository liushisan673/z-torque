"""MD-Skill 脚本运行时（内核调用方，**不含执行实现**）。

实现归属已下沉到插件 `app/builtin_plugins/sandbox_driver/`（标准插件可移植性）：
内核只保留 Port 抽象 `app/kernel/ports/sandbox_driver.py` 与本调用方。

语义变化（有意为之，不再静默降级）：
- 装配期由 `sandbox_driver` 插件注入 Port 实例，正常路径语义不变；
- **未注入实现时直接拒绝执行**（`AI_CAPABILITY_UNAVAILABLE`），而不是在内核里
  偷偷跑一份"默认实现"——那会让"插件只是空壳、实现在内核"的问题重新出现，
  也会让企业版换掉 Port 后仍有一条绕过路径。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

from app.core.errors import ApiError
from app.kernel.md_skill import MdSkillManifest, validate_input
from app.kernel.ports.sandbox_driver import SandboxDriver


class MdSkillRuntime:
    """Run only registered scripts; capability stays unavailable without isolation."""

    def __init__(self, root: str | Path, *, enabled: bool = False, timeout_seconds: float = 10, max_output_bytes: int = 65536, sandbox_driver: SandboxDriver | None = None) -> None:
        self.root = Path(root).resolve()
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        # sandbox.driver Port 由装配根经插件注入；缺省不构造任何实现（见模块 docstring）
        self._sandbox = sandbox_driver

    async def execute(self, manifest: MdSkillManifest, arguments: dict[str, Any]) -> dict[str, Any]:
        validate_input(manifest, arguments)
        if manifest.implementation != "md_script" or not manifest.allow_scripts:
            raise ApiError("AI_SKILL_SCRIPT_DISABLED", "script execution is not enabled for this skill", 503)
        if not self.enabled:
            raise ApiError("AI_CAPABILITY_UNAVAILABLE", "MD-Skill script runtime is disabled", 503)
        # Windows has no reliable process-level network/filesystem sandbox here.
        if os.name == "nt":
            raise ApiError("AI_CAPABILITY_UNAVAILABLE", "MD-Skill script isolation is unavailable on this platform", 503)
        if self._sandbox is None:
            # 能力缺失必须显式暴露：未装 sandbox_driver 插件时不允许执行脚本
            raise ApiError("AI_CAPABILITY_UNAVAILABLE", "no sandbox.driver implementation is resolved", 503)
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
