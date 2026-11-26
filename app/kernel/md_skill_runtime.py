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

from app.core.errors import SKILL_WINDOWS_UNTRUSTED, ApiError
from app.kernel.md_skill import MdSkillManifest, parse_skill, validate_input
from app.kernel.ports.sandbox_driver import SandboxDriver, SandboxScope


class MdSkillRuntime:
    """Run only registered scripts; capability stays unavailable without isolation."""

    def __init__(self, root: str | Path, *, enabled: bool = False, timeout_seconds: float = 10, max_output_bytes: int = 65536, sandbox_driver: SandboxDriver | None = None, windows_process_enabled: bool = False, allowed_roots: list[str] | None = None) -> None:
        self.root = Path(root).resolve()
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        # sandbox.driver Port 由装配根经插件注入；缺省不构造任何实现（见模块 docstring）
        self._sandbox = sandbox_driver
        # Windows 有限解禁（§2.3）：总开关 + 路径白名单强制收口
        self.windows_process_enabled = windows_process_enabled
        self.allowed_roots = [Path(item).expanduser().resolve() for item in (allowed_roots if allowed_roots is not None else [str(self.root)])]

    async def execute(self, manifest: MdSkillManifest, arguments: dict[str, Any], *, scope: SandboxScope | None = None, approval_granted: bool = False) -> dict[str, Any]:
        """执行脚本：scope 由 SkillGateway 显式传入（不依赖 ContextVar/环境继承）。

        直构场景未传时以驱动声明的边界为准，仍是显式对象透传，不是隐式回退。
        """
        if not approval_granted:
            raise ApiError("AI_SCRIPT_APPROVAL_REQUIRED", "MD-Skill script execution requires an approved Task checkpoint", 403)
        validate_input(manifest, arguments)
        if manifest.implementation != "md_script" or not manifest.allow_scripts:
            raise ApiError("AI_SKILL_SCRIPT_DISABLED", "script execution is not enabled for this skill", 503)
        if not self.enabled:
            raise ApiError("AI_CAPABILITY_UNAVAILABLE", "MD-Skill script runtime is disabled", 503)
        if os.name == "nt":
            # Windows 有限解禁（§2.3 / 2026-11-24 决策）：不是无条件放行，三重闸门缺一不可：
            #   1) 总开关 md_skill_windows_process_enabled；
            #   2) 技能 frontmatter 显式 windows_trusted=true（工具声明可信）；
            #   3) 路径白名单强制收口：脚本根目录必须落在 allowed_roots 内。
            # 用户确认由 SkillGateway 在调用本运行时前挂 checkpoint（execute_async 的
            # requires_confirmation），本层只负责平台闸门与路径边界。
            if not self.windows_process_enabled:
                raise ApiError("AI_CAPABILITY_UNAVAILABLE", "Windows process execution is disabled", 503)
            if not manifest.windows_trusted:
                raise ApiError(SKILL_WINDOWS_UNTRUSTED, "skill did not declare windows_trusted", 403)
        if len(manifest.script_files) != 1:
            raise ApiError("AI_SKILL_INVALID_MANIFEST", "exactly one script is required", 422)
        script = (self.root / manifest.relative_path / manifest.script_files[0]).resolve()
        skill_root = (self.root / manifest.relative_path).resolve()
        # 先校验授权根目录，避免无沙箱驱动时把越权路径错误地报告为普通能力缺失。
        in_allowlist = any(root == skill_root or root in skill_root.parents for root in self.allowed_roots)
        if not in_allowlist:
            raise ApiError("AI_SANDBOX_DENIED", "skill root is outside allowed sandbox roots", 403)
        if skill_root not in script.parents or script.suffix != ".py" or not script.is_file():
            raise ApiError("AI_SKILL_PATH_INVALID", "script path is invalid", 422)
        # 发布版本必须不可变：磁盘上的 Skill 文件与发布快照指纹不一致时拒绝执行，
        # 防止有人替换已发布脚本后借用旧版本审批继续运行新代码。
        try:
            current_manifest = parse_skill(self.root, manifest.relative_path)
        except ApiError as exc:
            raise ApiError("AI_SKILL_VERSION_DRIFT", "published MD-Skill files no longer match the published version", 409) from exc
        if (
            current_manifest.file_fingerprint != manifest.file_fingerprint
            or current_manifest.script_files != manifest.script_files
            or current_manifest.implementation != manifest.implementation
        ):
            raise ApiError("AI_SKILL_VERSION_DRIFT", "published MD-Skill files no longer match the published version", 409)
        if self._sandbox is None:
            # 能力缺失必须显式暴露：未装 sandbox_driver 插件时不允许执行脚本
            raise ApiError("AI_CAPABILITY_UNAVAILABLE", "no sandbox.driver implementation is resolved", 503)
        # 唯一执行入口：显式 scope 贯穿，驱动侧再做一次边界与隔离形态复校验
        effective_scope = scope or self._sandbox.declared_scope()
        result = await self._sandbox.execute(
            effective_scope, script_path=script, arguments=arguments, cwd=skill_root,
            timeout_seconds=self.timeout_seconds, max_output_bytes=self.max_output_bytes,
        )
        try:
            validate(result, manifest.output_schema)
        except ValidationError as exc:
            raise ApiError("AI_SKILL_FAILED", "MD-Skill output does not match schema", 500) from exc
        return result
