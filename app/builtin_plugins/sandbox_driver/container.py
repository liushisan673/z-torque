"""sandbox.driver 的容器隔离执行实现（v1.4 M4 §4.10）。

技术形态：docker CLI 子进程（零新 Python 依赖）。与进程内实现（process.py）同契约：
`run(*, script_path, arguments, cwd, timeout_seconds, max_output_bytes) -> dict`（stdout JSON），
错误码分类完全对齐（超时 AI_SKILL_TIMEOUT 504；资源超限 AI_SANDBOX_RESOURCE_EXCEEDED 429；
非零退出/非 JSON/输出超限 AI_SKILL_FAILED 500；cwd 越界 AI_SANDBOX_DENIED 403）。

容器层隔离面：
- ``--network none``：容器内无网络；
- ``--read-only`` + ``no-new-privileges`` + ``--cap-drop ALL``：根文件系统只读、禁止提权、丢弃全部 capability；
- ``--tmpfs /tmp``：只读根 fs 下仍给 Python 一个可写临时目录（否则 tempfile 直接失败）；
- ``--memory`` / ``--cpus`` / ``--pids-limit``：资源上限（对齐 rlimit 语义，命中 → 资源超限）；
- ``--cidfile``：记录容器 id，超时/输出超限时按 id `docker kill`，避免只杀 CLI 而容器继续跑；
- cwd 白名单语义不变：spawn 前先 ``enforce_cwd_policy``，越界拒绝；
- Windows 无容器运行时语义：装配期直接拒绝（fail-closed），运行期兜底同样拒绝。

镜像需预拉取：运行期不自动 pull，镜像缺失按 docker 退出码 125/127 归为脚本失败。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.builtin_plugins.sandbox_driver.process import (
    _communicate_capped,
    _terminate_process_tree,
    enforce_cwd_policy,
)
from app.core.errors import ApiError
from app.plugin_sdk import AssemblyError, SandboxDriver
from app.plugin_sdk.observability import emit_event

# docker 退出码语义：137 = SIGKILL（容器被 OOM killer 或显式 kill）；在资源限制已显式
# 配置的前提下归为"资源超限"而非泛化脚本失败。139 = SIGSEGV 属脚本自身崩溃（不是配额），
# 125/126/127 = daemon/命令层错误 → 同样归为脚本失败。
_CONTAINER_RESOURCE_EXIT_CODES = (137,)


class ContainerSandboxDriver(SandboxDriver):
    """sandbox.driver 的容器实现：docker run --network none + 只读根 fs + 资源上限。

    与 `ProcessSandboxDriver` 共享 cwd 白名单策略与错误码语义；`docker_binary` 可注入
    （单元测试用假 CLI），`emit` 可注入观测上报函数（缺省走 plugin_sdk 惰性转发）。
    """

    def __init__(
        self,
        *,
        image: str,
        memory: str,
        cpus: float,
        pids_limit: int,
        allowed_roots: list[str] | Sequence[str] | None = None,
        docker_binary: str = "docker",
        emit: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.allowed_roots = [str(Path(item).expanduser().resolve()) for item in (allowed_roots or [])]
        self.docker_binary = docker_binary
        self._emit = emit

    async def run(self, *, script_path: Path, arguments: dict[str, Any], cwd: Path, timeout_seconds: float, max_output_bytes: int) -> dict[str, Any]:
        return await run_containerized_process(
            script_path=script_path,
            arguments=arguments,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            image=self.image,
            memory=self.memory,
            cpus=self.cpus,
            pids_limit=self.pids_limit,
            allowed_roots=self.allowed_roots,
            docker_binary=self.docker_binary,
            emit=self._emit,
        )


def verify_docker_available(docker_binary: str = "docker", *, timeout_seconds: float = 10.0) -> None:
    """装配期探测：docker CLI 不存在或守护进程不可达 → AssemblyError。

    显式配置 container 却无法满足 = fail fast（与「白名单空 → 装配失败」同一风格），
    不允许带病启动后在运行期才发现无法执行。
    """
    try:
        completed = subprocess.run([docker_binary, "version", "--format", "ok"], capture_output=True, timeout=timeout_seconds, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssemblyError(f"docker CLI is not usable for sandbox_isolation_mode=container: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[:200]
        raise AssemblyError(f"docker daemon is not reachable for sandbox_isolation_mode=container: {detail or 'docker version failed'}")


def _container_mounts(workdir: Path, allowed_roots: Sequence[str]) -> list[str]:
    """挂载清单：白名单根只读挂载（去重、跳过与 workdir 相同者），workdir 本身可写。

    白名单根先挂、workdir 后挂：docker 按顺序处理 -v，更具体的 workdir 挂载
    覆盖其只读父目录挂载，脚本因此可写自己的 cwd、其余白名单区域只读。
    """
    mounts: list[str] = []
    seen: set[str] = {str(workdir)}
    for root in allowed_roots:
        resolved = str(Path(root).expanduser().resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        mounts.extend(("-v", f"{resolved}:{resolved}:ro"))
    mounts.extend(("-v", f"{workdir}:{workdir}"))
    return mounts


def _emit_container_meta(*, outcome: str, script_path: Path, duration_ms: int, exit_code: int | None, stdout_bytes: int, stderr_bytes: int, timed_out: bool, emit: Callable[[str, dict[str, Any]], None] | None = None) -> None:
    """向 observability.sink 发容器执行元信息（不含脚本内容、参数与输出正文）。

    键集合与进程内实现不同（`isolation` 替代 `rlimit_enabled`）：容器模式没有宿主
    rlimit 语义，隔离级别由容器参数承载。
    """
    payload = {
        "outcome": outcome,
        "script": script_path.name,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "timed_out": timed_out,
        "isolation": "container",
        "platform": os.name,
    }
    (emit or emit_event)("sandbox.execution", payload)


def _resolve_container_paths(script_path: Path, cwd: Path, allowed_roots: Sequence[str]) -> tuple[Path, Path]:
    """脚本与工作目录绝对化（容器挂载点 = 宿主路径）+ 脚本可见性校验。

    容器内只挂载白名单根与 workdir：脚本若落在这两者之外，容器里根本看不到——
    与其让 docker 报 125/127 再归为"脚本失败"，不如装配语义上直接按策略拒绝。
    """
    script = Path(script_path).expanduser().resolve()
    workdir = Path(cwd).expanduser().resolve()
    for root in allowed_roots:
        root_path = Path(root).expanduser().resolve()
        if script == root_path or root_path in script.parents:
            return script, workdir
    raise ApiError("AI_SANDBOX_DENIED", "sandbox script must reside under allowed roots for container execution", 403)


def _new_cidfile() -> Path:
    """容器 id 文件路径（docker 要求文件不存在，故只生成路径不创建文件）。"""
    return Path(tempfile.gettempdir()) / f"zt-sandbox-{uuid.uuid4().hex}.cid"


async def _kill_container(docker_binary: str, cid_file: Path) -> None:
    """按 cidfile 里的容器 id 强杀容器（best-effort，清理路径不得抛出）。"""
    try:
        container_id = cid_file.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if not container_id:
        return
    try:
        killer = await asyncio.create_subprocess_exec(
            docker_binary,
            "kill",
            container_id,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(killer.wait(), timeout=10)
    except (OSError, asyncio.TimeoutError):  # pragma: no cover - 清理路径尽力而为
        pass
    finally:
        cid_file.unlink(missing_ok=True)


async def run_containerized_process(
    *,
    script_path: Path,
    arguments: dict[str, Any],
    cwd: Path,
    timeout_seconds: float,
    max_output_bytes: int,
    image: str,
    memory: str,
    cpus: float,
    pids_limit: int,
    allowed_roots: Sequence[str] | None = None,
    docker_binary: str = "docker",
    emit: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """容器内执行单脚本：stdin 传 JSON 参数、stdout 收 JSON 结果（sandbox.driver 容器实现核心）。"""
    if os.name == "nt":
        # 运行期兜底：Windows 装配期已拒绝 container 模式（fail-closed），这里不再放行
        raise ApiError("AI_SANDBOX_DENIED", "container sandbox isolation is not supported on Windows", 403)
    resolved_roots = list(allowed_roots or [])
    enforce_cwd_policy(cwd, resolved_roots)
    script, workdir = _resolve_container_paths(script_path, cwd, resolved_roots)
    cid_file = _new_cidfile()
    command = [
        docker_binary,
        "run",
        "--rm",
        "-i",
        "--network",
        "none",
        "--read-only",
        "--security-opt",
        "no-new-privileges",
        "--cap-drop",
        "ALL",
        "--tmpfs",
        "/tmp:rw,nosuid,size=64m",
        "--cidfile",
        str(cid_file),
        "--memory",
        memory,
        "--cpus",
        str(cpus),
        "--pids-limit",
        str(pids_limit),
        *_container_mounts(workdir, list(allowed_roots or [])),
        "-w",
        str(workdir),
        image,
        "python",
        "-I",
        str(script),
    ]
    spawn_kwargs: dict[str, Any] = {}
    if os.name != "nt":
        spawn_kwargs["start_new_session"] = True
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **spawn_kwargs)

    async def _terminate() -> None:
        # 先杀 CLI 进程组，再按 cidfile 杀容器：只杀 CLI 时容器可能继续运行
        await _terminate_process_tree(process)
        await _kill_container(docker_binary, cid_file)

    try:
        stdout, stderr, output_exceeded, timed_out = await _communicate_capped(
            process=process,
            payload=json.dumps(arguments).encode(),
            max_output_bytes=max_output_bytes,
            timeout_seconds=timeout_seconds,
            terminate=_terminate,
        )
    finally:
        cid_file.unlink(missing_ok=True)
    duration_ms = int((time.monotonic() - started) * 1000)
    if timed_out:
        _emit_container_meta(outcome="timeout", script_path=script, duration_ms=duration_ms, exit_code=None, stdout_bytes=len(stdout), stderr_bytes=len(stderr), timed_out=True, emit=emit)
        raise ApiError("AI_SKILL_TIMEOUT", "MD-Skill script timed out", 504)
    if output_exceeded:
        _emit_container_meta(outcome="output_exceeded", script_path=script, duration_ms=duration_ms, exit_code=process.returncode, stdout_bytes=len(stdout), stderr_bytes=len(stderr), timed_out=False, emit=emit)
        raise ApiError("AI_SKILL_FAILED", "MD-Skill script output exceeded limit", 500)
    if process.returncode != 0:
        # 容器资源上限（OOM/SIGKILL）必须归为"资源超限"；daemon/命令层错误与业务失败归"脚本失败"
        resource_killed = process.returncode in _CONTAINER_RESOURCE_EXIT_CODES
        _emit_container_meta(outcome="resource_exceeded" if resource_killed else "failed", script_path=script, duration_ms=duration_ms, exit_code=process.returncode, stdout_bytes=len(stdout), stderr_bytes=len(stderr), timed_out=False, emit=emit)
        if resource_killed:
            raise ApiError("AI_SANDBOX_RESOURCE_EXCEEDED", "MD-Skill script exceeded sandbox resource limits", 429)
        raise ApiError("AI_SKILL_FAILED", "MD-Skill script failed", 500)
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        _emit_container_meta(outcome="invalid_output", script_path=script, duration_ms=duration_ms, exit_code=process.returncode, stdout_bytes=len(stdout), stderr_bytes=len(stderr), timed_out=False, emit=emit)
        raise ApiError("AI_SKILL_FAILED", "MD-Skill script output is not valid JSON", 500) from exc
    if not isinstance(payload, dict):
        _emit_container_meta(outcome="invalid_output", script_path=script, duration_ms=duration_ms, exit_code=process.returncode, stdout_bytes=len(stdout), stderr_bytes=len(stderr), timed_out=False, emit=emit)
        raise ApiError("AI_SKILL_FAILED", "MD-Skill script output must be a JSON object", 500)
    _emit_container_meta(outcome="ok", script_path=script, duration_ms=duration_ms, exit_code=process.returncode, stdout_bytes=len(stdout), stderr_bytes=len(stderr), timed_out=False, emit=emit)
    return payload
