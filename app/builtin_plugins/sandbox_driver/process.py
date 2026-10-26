"""sandbox.driver 的进程内受限执行实现（v1.3 §4.3）。

**归属变更（标准插件可移植性）**：本实现原先躺在 `app/kernel/md_skill_runtime.py`，
插件只是登记转发，等于"实现在内核、插件是空壳"。现整体下沉到插件目录：

- 内核只保留 Port 抽象（`app/kernel/ports/sandbox_driver.py`）与调用方 `MdSkillRuntime`；
- 内核不再包含任何可执行实现，未注入 Port 时 `MdSkillRuntime` 直接拒绝执行
  （`AI_CAPABILITY_UNAVAILABLE`），不会静默降级为无隔离执行；
- 企业版可用 PLUGIN/EXPLICIT 优先级换成容器/sidecar 实现，本文件可整体不带走。

加固要点：
- cwd 白名单：allowed_roots 非空时 cwd 越界 → AI_SANDBOX_DENIED；
- 进程组终止：POSIX 以 start_new_session 建独立进程组，超时用 killpg(SIGKILL) 清理整棵树；
- POSIX rlimit：CPU / FSIZE / NPROC，命中即 AI_SANDBOX_RESOURCE_EXCEEDED；
- observability.sink 元信息（脚本标识、耗时、退出码、字节数），不记录脚本内容与参数。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any

from app.core.errors import ApiError
from app.plugin_sdk import SandboxDriver
from app.plugin_sdk.observability import emit_event


class ProcessSandboxDriver(SandboxDriver):
    """sandbox.driver 的进程内实现：独立解释器 -I + 最小环境变量 + cwd 白名单 + rlimit。

    平台降级：Windows 无 rlimit 与进程组语义，超时仅 process.kill()（子进程可能残留，
    v1 明确不承诺强隔离），由 MdSkillRuntime 在 Windows 上直接拒绝脚本执行。
    """

    def __init__(
        self,
        *,
        allowed_roots: list[str] | Sequence[str] | None = None,
        rlimit_enabled: bool = False,
        max_processes: int = 256,
        max_file_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        # rlimit 默认关闭以保持直构/单元测试语义不变；装配期由本插件按
        # SANDBOX_RLIMIT_ENABLED 显式开启
        self.allowed_roots = [str(Path(item).expanduser().resolve()) for item in (allowed_roots or [])]
        self.rlimit_enabled = rlimit_enabled
        self.max_processes = max_processes
        self.max_file_bytes = max_file_bytes

    async def run(self, *, script_path: Path, arguments: dict[str, Any], cwd: Path, timeout_seconds: float, max_output_bytes: int) -> dict[str, Any]:
        return await run_isolated_process(
            script_path=script_path,
            arguments=arguments,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            allowed_roots=self.allowed_roots,
            rlimit_enabled=self.rlimit_enabled,
            max_processes=self.max_processes,
            max_file_bytes=self.max_file_bytes,
        )


def enforce_cwd_policy(cwd: Path, allowed_roots: Sequence[str]) -> None:
    """cwd 白名单校验：越界即策略拒绝（不依赖符号链接解析后的真实路径）。"""
    if not allowed_roots:
        return
    resolved = Path(cwd).expanduser().resolve()
    for root in allowed_roots:
        root_path = Path(root).expanduser().resolve()
        if resolved == root_path or root_path in resolved.parents:
            return
    raise ApiError("AI_SANDBOX_DENIED", "sandbox working directory is outside allowed roots", 403)


def _apply_rlimit(*, cpu_seconds: int, max_file_bytes: int, max_processes: int) -> None:
    """POSIX 资源上限（子进程 exec 前调用）；平台不支持时静默跳过。"""
    try:
        import resource  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - Windows
        return
    limits = (
        ("RLIMIT_CPU", max(1, int(cpu_seconds)), max(2, int(cpu_seconds) + 1)),
        ("RLIMIT_FSIZE", int(max_file_bytes), int(max_file_bytes)),
        ("RLIMIT_NPROC", int(max_processes), int(max_processes)),
    )
    for name, soft, hard in limits:
        identifier = getattr(resource, name, None)
        if identifier is None:
            continue
        try:
            resource.setrlimit(identifier, (soft, hard))
        except (ValueError, OSError):  # pragma: no cover - 受限容器内可能不允许
            continue


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """超时清理：POSIX 杀整个进程组，Windows 仅杀直接子进程（v1 平台降级）。"""
    if process.returncode is not None:
        return
    if os.name != "nt":
        import signal

        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):  # pragma: no cover - 进程已退出
            process.kill()
    else:
        # Windows 无可靠进程组终止：process.kill() 不保证孙进程退出，属已声明的平台限制
        process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:  # pragma: no cover - 极端场景，避免清理阶段悬挂
        pass


# Linux 上 CPython 启动时把 SIGXFSZ 置为 SIG_IGN（避免大文件写入直接终结解释器），
# 子进程继承了同样的处置，于是 RLIMIT_FSIZE 命中不会体现为 "被信号杀死"，而是
# write 返回 EFBIG 抛 OSError 退出。父进程侧只能靠 errno 文案兜底识别。
_RESOURCE_ERRNO_MARKERS: tuple[tuple[bytes, str], ...] = (
    (b"[Errno 27]", "file_size"),  # EFBIG：超过 RLIMIT_FSIZE
    (b"[Errno 11]", "process_count"),  # EAGAIN：超过 RLIMIT_NPROC 时 fork 失败
)


def _resource_limit_hit(*, returncode: int | None, stderr: bytes, rlimit_enabled: bool) -> bool:
    """判定本次执行是否命中沙箱资源上限。

    两条判据并存，缺一不可：
    1. 退出码为负数（SIGKILL/SIGXCPU/SIGXFSZ）—— 由内核直接终结；
    2. rlimit 开启且 stderr 出现内核 errno 文案 —— 子进程忽略/捕获了信号的情形。

    只匹配内核生成的 errno 文案，不匹配脚本自定义输出，避免把业务失败误报为配额。
    """
    if returncode in (-9, -24, -25):
        return True
    if not rlimit_enabled:
        return False
    return any(marker in stderr for marker, _ in _RESOURCE_ERRNO_MARKERS)


def _emit_sandbox_meta(*, outcome: str, script_path: Path, duration_ms: int, exit_code: int | None, stdout_bytes: int, stderr_bytes: int, timed_out: bool, rlimit_enabled: bool, emit: Callable[[str, dict[str, Any]], None] | None = None) -> None:
    """向 observability.sink 发沙箱执行元信息（不含脚本内容、参数与输出正文）。

    `emit` 为可注入的上报函数（装配期传 SDK 实现），缺省走 `app.plugin_sdk.observability`
    的惰性转发——插件因此不需要 import 内核注册表。
    """
    payload = {
        "outcome": outcome,
        "script": script_path.name,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "timed_out": timed_out,
        "rlimit_enabled": rlimit_enabled,
        "platform": os.name,
    }
    (emit or emit_event)("sandbox.execution", payload)


async def run_isolated_process(
    *,
    script_path: Path,
    arguments: dict[str, Any],
    cwd: Path,
    timeout_seconds: float,
    max_output_bytes: int,
    allowed_roots: Sequence[str] | None = None,
    rlimit_enabled: bool = False,
    max_processes: int = 256,
    max_file_bytes: int = 16 * 1024 * 1024,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """受控执行单脚本：stdin 传 JSON 参数、stdout 收 JSON 结果（sandbox.driver 实现核心）。"""
    enforce_cwd_policy(cwd, list(allowed_roots or []))
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONNOUSERSITE": "1", "PYTHONIOENCODING": "utf-8"}
    spawn_kwargs: dict[str, Any] = {}
    if os.name != "nt":
        spawn_kwargs["start_new_session"] = True
        if rlimit_enabled:
            spawn_kwargs["preexec_fn"] = partial(
                _apply_rlimit,
                cpu_seconds=int(timeout_seconds) or 1,
                max_file_bytes=max_file_bytes,
                max_processes=max_processes,
            )
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(sys.executable, "-I", str(script_path), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(cwd), env=env, **spawn_kwargs)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(arguments).encode()), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        await _terminate_process_tree(process)
        _emit_sandbox_meta(outcome="timeout", script_path=script_path, duration_ms=int((time.monotonic() - started) * 1000), exit_code=None, stdout_bytes=0, stderr_bytes=0, timed_out=True, rlimit_enabled=rlimit_enabled, emit=emit)
        raise ApiError("AI_SKILL_TIMEOUT", "MD-Skill script timed out", 504)
    duration_ms = int((time.monotonic() - started) * 1000)
    if len(stdout) > max_output_bytes or len(stderr) > max_output_bytes:
        _emit_sandbox_meta(outcome="output_exceeded", script_path=script_path, duration_ms=duration_ms, exit_code=process.returncode, stdout_bytes=len(stdout), stderr_bytes=len(stderr), timed_out=False, rlimit_enabled=rlimit_enabled, emit=emit)
        raise ApiError("AI_SKILL_FAILED", "MD-Skill script output exceeded limit", 500)
    if process.returncode != 0:
        # rlimit 命中必须归为"资源超限"而不是泛化的脚本失败，否则调用方无法区分
        # 自身 bug 与配额耗尽；判定同时覆盖信号与 errno 两条路径（见 _resource_limit_hit）
        resource_killed = _resource_limit_hit(returncode=process.returncode, stderr=stderr, rlimit_enabled=rlimit_enabled)
        _emit_sandbox_meta(outcome="resource_exceeded" if resource_killed else "failed", script_path=script_path, duration_ms=duration_ms, exit_code=process.returncode, stdout_bytes=len(stdout), stderr_bytes=len(stderr), timed_out=False, rlimit_enabled=rlimit_enabled, emit=emit)
        if resource_killed:
            raise ApiError("AI_SANDBOX_RESOURCE_EXCEEDED", "MD-Skill script exceeded sandbox resource limits", 429)
        raise ApiError("AI_SKILL_FAILED", "MD-Skill script failed", 500)
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        # 脚本输出非 JSON 属"执行结果不合约"，必须转为 ApiError，不能冒泡成 500 未处理异常
        _emit_sandbox_meta(outcome="invalid_output", script_path=script_path, duration_ms=duration_ms, exit_code=process.returncode, stdout_bytes=len(stdout), stderr_bytes=len(stderr), timed_out=False, rlimit_enabled=rlimit_enabled, emit=emit)
        raise ApiError("AI_SKILL_FAILED", "MD-Skill script output is not valid JSON", 500) from exc
    if not isinstance(payload, dict):
        raise ApiError("AI_SKILL_FAILED", "MD-Skill script output must be a JSON object", 500)
    _emit_sandbox_meta(outcome="ok", script_path=script_path, duration_ms=duration_ms, exit_code=process.returncode, stdout_bytes=len(stdout), stderr_bytes=len(stderr), timed_out=False, rlimit_enabled=rlimit_enabled, emit=emit)
    return payload
