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


class MdSkillRuntime:
    """Run only registered scripts; capability stays unavailable without isolation."""

    def __init__(self, root: str | Path, *, enabled: bool = False, timeout_seconds: float = 10, max_output_bytes: int = 65536) -> None:
        self.root = Path(root).resolve()
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

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
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONNOUSERSITE": "1"}
        process = await asyncio.create_subprocess_exec(sys.executable, "-I", str(script), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(skill_root), env=env)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(arguments).encode()), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise ApiError("AI_SKILL_TIMEOUT", "MD-Skill script timed out", 504)
        if len(stdout) > self.max_output_bytes or len(stderr) > self.max_output_bytes:
            raise ApiError("AI_SKILL_FAILED", "MD-Skill script output exceeded limit", 500)
        if process.returncode != 0:
            raise ApiError("AI_SKILL_FAILED", "MD-Skill script failed", 500)
        try:
            result = json.loads(stdout)
            validate(result, manifest.output_schema)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ApiError("AI_SKILL_FAILED", "MD-Skill output does not match schema", 500) from exc
        return result
