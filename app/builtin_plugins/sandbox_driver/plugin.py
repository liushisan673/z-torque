"""sandbox.driver Port 的内置实现插件：装配登记 + 策略配置（v1.3 §4.4，契约 §3.5）。

实现本体在本包的 `process.py`（原先在内核 `app/kernel/md_skill_runtime.py`，已下沉）；
本模块只做装配期策略：

- SANDBOX_ALLOWED_ROOTS 为空 → 收窄为 MD_SKILLS_DIR（最小权限白名单）；
- SANDBOX_RLIMIT_ENABLED 仅在 POSIX 生效；Windows 缺省跳过并在 capabilities 暴露；
- 白名单为空字符串项直接忽略；非绝对路径按当前工作目录解析，装配期即定稿。
"""

from pathlib import Path

from app.builtin_plugins.sandbox_driver.process import ProcessSandboxDriver
from app.plugin_sdk import AssemblyError, PluginManifest


class SandboxDriverPlugin:
    manifest = PluginManifest(
        name="sandbox_driver",
        description="进程内受限脚本执行（独立解释器 -I + 最小环境变量 + cwd 白名单 + rlimit）",
        provides=("sandbox.driver",),
        permissions=("process.spawn",),
        # 结构化 JSON Schema：装配期按 Settings 投影校验（类型/枚举/必填），
        # 语义校验（空白名单拒绝、上限为正）仍由 contribute 内的策略完成。
        config_schema={
            "type": "object",
            "properties": {
                "sandbox_allowed_roots": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "default": None,
                    "description": "cwd 白名单；None → [MD_SKILLS_DIR]，[] → 装配期拒绝（空 = 不限制语义反转为拒绝）",
                },
                "sandbox_rlimit_enabled": {
                    "type": "boolean",
                    "default": True,
                    "description": "POSIX rlimit 开关（Windows 无 rlimit 语义，按平台跳过并在 capabilities 暴露）",
                },
                "sandbox_max_processes": {
                    "type": "integer",
                    "default": 64,
                    "description": "RLIMIT_NPROC 软硬限",
                },
                "sandbox_max_file_bytes": {
                    "type": "integer",
                    "default": 16777216,
                    "description": "RLIMIT_FSIZE 软硬限（默认 16 MiB）",
                },
            },
            "additionalProperties": False,
        },
    )

    @staticmethod
    def _resolve_roots(settings) -> list[str]:
        """白名单解析：优先 Settings 的 resolved_sandbox_roots 属性，其次按三态语义自行推导。

        直构/测试场景传入的轻量 settings 可能没有该属性；此时按
        `sandbox_allowed_roots=None → [md_skills_dir]` 的同一语义回落，
        但**显式空列表**一律返回空（由调用方 AssemblyError）。
        """
        resolved = getattr(settings, "resolved_sandbox_roots", None)
        if resolved is not None:
            return list(resolved)
        raw = getattr(settings, "sandbox_allowed_roots", None)
        if raw is None:
            md_dir = getattr(settings, "md_skills_dir", None)
            return [str(md_dir)] if md_dir else []
        return [str(item) for item in raw if str(item).strip()]

    def contribute(self, sdk) -> None:
        settings = sdk.settings
        roots = self._resolve_roots(settings)
        if not roots:
            # 装配期 fail fast：没有白名单等于没有 cwd 策略（v1.3 §4.4「空 = 拒绝」语义）
            raise AssemblyError("plugin sandbox_driver requires a non-empty sandbox_allowed_roots")
        normalized = [str(Path(item).expanduser().resolve()) for item in roots]
        rlimit_enabled = bool(getattr(settings, "sandbox_rlimit_enabled", True))
        driver = ProcessSandboxDriver(
            allowed_roots=normalized,
            rlimit_enabled=rlimit_enabled,
            max_processes=int(getattr(settings, "sandbox_max_processes", 256)),
            max_file_bytes=int(getattr(settings, "sandbox_max_file_bytes", 16 * 1024 * 1024)),
        )
        # 装配期对象共享：skill_executors 构造 SkillGateway 时注入 MdSkillRuntime
        sdk.publish("sandbox.driver", driver)
        sdk.publish("sandbox.allowed_roots", tuple(normalized))
        sdk.register_port(
            "sandbox.driver",
            lambda: driver,
            label=f"builtin_inprocess_sandbox(rlimit={rlimit_enabled})",
        )
