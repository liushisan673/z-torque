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
- 输出上限：流式读取并累计，超限立即终止子进程（不做"先全量缓冲再判长度"，避免大输出打爆内存）；
- observability.sink 元信息（脚本标识、耗时、退出码、字节数），不记录脚本内容与参数。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
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
        max_processes: int = 64,
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


def _force_stream_eof(process: asyncio.subprocess.Process) -> None:
    """终止后主动给输出流喂 EOF。

    Windows proactor 上，子进程被 TerminateProcess 之后，若 stdout 管道里仍有未消费的
    数据（典型场景就是"输出超限/超时被我们杀掉"），transport 不会向后唤醒 `read()` 与
    `wait()`：表现为 returncode 已是 1 但 `await process.wait()` 一直挂住。主动 feed_eof
    让读端立刻收尾，避免清理阶段白等。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(process, name, None)
        if stream is None:  # pragma: no cover - 恒有管道
            continue
        try:
            stream.feed_eof()
        except (RuntimeError, ValueError):  # pragma: no cover - 流已关闭
            pass


async def _wait_exit(process: asyncio.subprocess.Process, *, timeout: float = 2.0) -> None:
    """有界等待进程退出；平台缺陷导致 wait() 不唤醒时不超过 timeout 秒。"""
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:  # pragma: no cover - 见 _force_stream_eof 的说明
        pass


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
    _force_stream_eof(process)
    await _wait_exit(process)


_OUTPUT_CHUNK_BYTES = 64 * 1024


async def _write_stdin(stream: asyncio.StreamWriter | None, payload: bytes) -> None:
    """写入参数并关闭 stdin：脚本以 `sys.stdin.read()` 等待 EOF，不关闭会一直挂到超时。"""
    if stream is None:  # pragma: no cover - create_subprocess 恒给管道
        return
    try:
        stream.write(payload)
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        # 脚本未读 stdin 就退出：属正常竞争，不是执行失败
        pass
    finally:
        try:
            stream.close()
        except (RuntimeError, OSError):  # pragma: no cover - 已关闭
            pass


async def _pump_stream(stream: asyncio.StreamReader | None, limit: int, exceeded: asyncio.Event) -> tuple[bytes, bool]:
    """流式读取单个输出流，累计不超过 limit 字节；越界即置位事件并返回部分内容。"""
    if stream is None:  # pragma: no cover - create_subprocess 恒给管道
        return b"", False
    buffer = bytearray()
    while True:
        try:
            chunk = await stream.read(_OUTPUT_CHUNK_BYTES)
        except (ConnectionResetError, BrokenPipeError):  # pragma: no cover - 子进程被清理时
            return bytes(buffer), False
        if not chunk:
            return bytes(buffer), False
        remaining = limit - len(buffer)
        if len(chunk) > remaining:
            if remaining > 0:
                buffer.extend(chunk[:remaining])
            exceeded.set()
            return bytes(buffer), True
        buffer.extend(chunk)


async def _communicate_capped(
    *,
    process: asyncio.subprocess.Process,
    payload: bytes,
    max_output_bytes: int,
    timeout_seconds: float,
    terminate: Callable[[], Awaitable[None]] | None = None,
) -> tuple[bytes, bytes, bool, bool]:
    """带输出上限的 communicate：返回 (stdout, stderr, output_exceeded, timed_out)。

    与 `process.communicate()` 的语义差异：stdout/stderr 边读边计数，单个流一旦超过
    `max_output_bytes` 立即触发 `terminate`（而非先把全部输出读进内存再判长度），
    因此大输出不会造成父进程内存放大，也不会让子进程在写满管道后白白挂到超时。
    """
    captured: dict[str, tuple[bytes, bool]] = {"stdout": (b"", False), "stderr": (b"", False)}
    exceeded = asyncio.Event()

    async def _pump(name: str) -> None:
        captured[name] = await _pump_stream(getattr(process, name), max_output_bytes, exceeded)

    async def _watch() -> None:
        await exceeded.wait()
        await (terminate or partial(_terminate_process_tree, process))()

    tasks = [
        asyncio.create_task(_pump("stdout")),
        asyncio.create_task(_pump("stderr")),
        asyncio.create_task(_write_stdin(process.stdin, payload)),
    ]
    watcher = asyncio.create_task(_watch())
    # 注意：不要把 process.wait() 放进 gather。Windows proactor 在"管道被写满后被杀"
    # 的场景下不会唤醒 wait()（returncode 已有值但协程挂住），以读端 EOF 作为完成信号更可靠
    waiter = asyncio.gather(*tasks, return_exceptions=True)
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout_seconds)
        if not exceeded.is_set():
            # 正常结束才补退出码；被我们 kill 的路径见 _force_stream_eof（wait 可能不唤醒）
            await _wait_exit(process)
    except asyncio.TimeoutError:
        timed_out = True
        await (terminate or partial(_terminate_process_tree, process))()
        try:
            # 终止后让读端把已缓冲内容读完，超时/截断时仍能带上诊断字节数
            await asyncio.wait_for(asyncio.shield(waiter), timeout=3)
        except asyncio.TimeoutError:  # pragma: no cover - 管道被异常占住
            for task in tasks:
                task.cancel()
            waiter.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    stdout, stdout_exceeded = captured["stdout"]
    stderr, stderr_exceeded = captured["stderr"]
    return stdout, stderr, stdout_exceeded or stderr_exceeded, timed_out


# Linux 上 CPython 启动时把 SIGXFSZ 置为 SIG_IGN（避免大文件写入直接终结解释器），
# 子进程继承了同样的处置，于是 RLIMIT_FSIZE 命中不会体现为 "被信号杀死"，而是
# write 返回 EFBIG 抛 OSError 退出。父进程侧只能靠 errno 文案兜底识别。
_RESOURCE_ERRNO_MARKERS: tuple[tuple[bytes, str], ...] = (
    (b"[Errno 27]", "file_size"),  # EFBIG：超过 RLIMIT_FSIZE
    (b"[Errno 11]", "process_count"),  # EAGAIN：超过 RLIMIT_NPROC 时 fork 失败
)


def _resource_limit_hit(*, returncode: int | None, stderr: bytes, rlimit_enabled: bool) -> bool:
    """判定本次执行是否命中沙箱资源上限。

    判定前提是 rlimit 已开启：关闭时任何信号退出都只是普通执行失败，不能报成配额耗尽。
    开启后两条判据并存：
    1. SIGXCPU(-24) / SIGXFSZ(-25) —— 由内核按 rlimit 直接终结，语义唯一；
    2. 裸 SIGKILL(-9) 或 errno 文案 —— 需 stderr 出现内核 errno（EFBIG/EAGAIN）才算命中，
       因为 -9 也可能来自外部 kill 或 OOM killer，不能无条件归为沙箱配额。

    只匹配内核生成的 errno 文案，不匹配脚本自定义输出，避免把业务失败误报为配额。
    """
    if not rlimit_enabled:
        return False
    if returncode in (-24, -25):
        return True
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
    max_processes: int = 64,
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
    stdout, stderr, output_exceeded, timed_out = await _communicate_capped(
        process=process,
        payload=json.dumps(arguments).encode(),
        max_output_bytes=max_output_bytes,
        timeout_seconds=timeout_seconds,
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    if timed_out:
        _emit_sandbox_meta(outcome="timeout", script_path=script_path, duration_ms=duration_ms, exit_code=None, stdout_bytes=len(stdout), stderr_bytes=len(stderr), timed_out=True, rlimit_enabled=rlimit_enabled, emit=emit)
        raise ApiError("AI_SKILL_TIMEOUT", "MD-Skill script timed out", 504)
    if output_exceeded:
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
        _emit_sandbox_meta(outcome="invalid_output", script_path=script_path, duration_ms=duration_ms, exit_code=process.returncode, stdout_bytes=len(stdout), stderr_bytes=len(stderr), timed_out=False, rlimit_enabled=rlimit_enabled, emit=emit)
        raise ApiError("AI_SKILL_FAILED", "MD-Skill script output must be a JSON object", 500)
    _emit_sandbox_meta(outcome="ok", script_path=script_path, duration_ms=duration_ms, exit_code=process.returncode, stdout_bytes=len(stdout), stderr_bytes=len(stderr), timed_out=False, rlimit_enabled=rlimit_enabled, emit=emit)
    return payload
