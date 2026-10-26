"""M1 沙箱加固专项单测（v1.3 §4.3 / §4.4）。

之前的验证只覆盖"能跑通"，本文件补上策略与失败分支：
1. cwd 白名单：越界必须拒绝且**不落盘**（脚本根本没被执行）；
2. POSIX rlimit：FSIZE 命中 → AI_SANDBOX_RESOURCE_EXCEEDED 429（非泛化 AI_SKILL_FAILED）；
3. POSIX 进程组终止：超时后孙进程必须一并清理（killpg），不能只杀父进程；
4. 输出上限 / 非零退出 / 非 JSON / 非对象输出 → 稳定的 ApiError；
5. observability 元信息：事件名与字段集合固定，且**不含**脚本内容与参数；
6. 装配期三态白名单：None → MD_SKILLS_DIR 回落、[] → 拒绝装配、非空 → 校验。

Windows 上 rlimit 与进程组语义不存在，相关用例 skip 并显式说明（跳过≠通过）。
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.builtin_plugins.sandbox_driver import (
    ProcessSandboxDriver,
    SandboxDriverPlugin,
    enforce_cwd_policy,
    run_isolated_process,
)
from app.core.errors import ApiError
from app.kernel.md_skill import MdSkillManifest
from app.kernel.md_skill_runtime import MdSkillRuntime
from app.kernel.registry.port_registry import AssemblyError
from tests.support.fakes import FakeSdk

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX rlimit / killpg 分支只能在 Linux 上验证")

_ECHO = """
import json, sys
payload = json.loads(sys.stdin.read() or "{}")
print(json.dumps({"echo": payload}))
"""


def _script(root, name: str, body: str):
    path = root / name
    path.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
    return path


def _process_alive(pid: int) -> bool:
    """进程是否仍"活着"（僵尸不算）。

    `os.kill(pid, 0)` 对僵尸进程同样返回成功，而僵尸是否消失取决于父进程是否回收。
    容器里 PID 1 常常不是 init（如 `sleep infinity`）而永不回收，僵尸会一直留在进程表，
    因此必须读 /proc/<pid>/stat 的状态位：Z 表示已被杀死、只是无人 wait。
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - 非 root 运行且进程属他人
        return True
    stat = Path(f"/proc/{pid}/stat")
    try:
        # 格式: pid (comm) state ...；comm 可能含空格与括号，故从最后一个 ')' 之后切分
        state = stat.read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):  # pragma: no cover - 无 /proc 的平台按存活处理
        return True
    return state != "Z"


# ---------------------------------------------------------------- cwd 白名单


def test_cwd_policy_allows_nested_paths_and_rejects_escapes(tmp_path):
    root = tmp_path / "skills"
    nested = root / "demo" / "sub"
    nested.mkdir(parents=True)

    enforce_cwd_policy(root, [str(root)])
    enforce_cwd_policy(nested, [str(root)])
    enforce_cwd_policy(tmp_path, [])  # 内核默认：未配置白名单即不限制（装配层保证非空）

    with pytest.raises(ApiError) as excinfo:
        enforce_cwd_policy(tmp_path / "outside", [str(root)])
    assert excinfo.value.code == "AI_SANDBOX_DENIED"
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_cwd_escape_is_rejected_before_spawning(tmp_path):
    allowed = tmp_path / "allowed"
    denied = tmp_path / "denied"
    allowed.mkdir()
    denied.mkdir()
    marker = tmp_path / "executed.txt"
    script = _script(allowed, "mark.py", f"""
        import pathlib
        pathlib.Path({str(marker)!r}).write_text("ran")
        print("{{}}")
    """)

    with pytest.raises(ApiError) as excinfo:
        await run_isolated_process(
            script_path=script,
            arguments={},
            cwd=denied,
            timeout_seconds=5,
            max_output_bytes=4096,
            allowed_roots=[str(allowed)],
        )

    assert excinfo.value.code == "AI_SANDBOX_DENIED"
    assert not marker.exists(), "越界 cwd 必须在 spawn 之前拒绝，不能先跑再判"


# ---------------------------------------------------------------- 执行结果


@pytest.mark.asyncio
async def test_success_returns_script_json(tmp_path):
    root = tmp_path / "skill"
    root.mkdir()
    script = _script(root, "echo.py", _ECHO)

    result = await run_isolated_process(
        script_path=script, arguments={"q": 1}, cwd=root, timeout_seconds=10, max_output_bytes=4096, allowed_roots=[str(root)]
    )

    assert result == {"echo": {"q": 1}}


@pytest.mark.asyncio
async def test_timeout_is_504_and_meta_marks_timeout(tmp_path, monkeypatch):
    root = tmp_path / "skill"
    root.mkdir()
    script = _script(root, "slow.py", "import time; time.sleep(30)")
    events = []
    monkeypatch.setattr("app.kernel.registry.active.emit_event", lambda event, payload=None: events.append((event, payload)))

    with pytest.raises(ApiError) as excinfo:
        await run_isolated_process(
            script_path=script, arguments={}, cwd=root, timeout_seconds=0.3, max_output_bytes=4096, allowed_roots=[str(root)]
        )

    assert excinfo.value.code == "AI_SKILL_TIMEOUT"
    assert excinfo.value.status_code == 504
    assert events[-1][1]["outcome"] == "timeout"
    assert events[-1][1]["timed_out"] is True


@pytest.mark.asyncio
async def test_output_limit_and_nonzero_exit_and_bad_json(tmp_path):
    root = tmp_path / "skill"
    root.mkdir()
    noisy = _script(root, "noisy.py", "print('x' * 5000)")
    failing = _script(root, "fail.py", "import sys; sys.exit(3)")
    bad_json = _script(root, "bad_json.py", "print('not json')")
    list_json = _script(root, "list_json.py", "print('[1, 2]')")

    async def run(script, max_output_bytes=4096):
        return await run_isolated_process(
            script_path=script, arguments={}, cwd=root, timeout_seconds=10, max_output_bytes=max_output_bytes, allowed_roots=[str(root)]
        )

    with pytest.raises(ApiError) as excinfo:
        await run(noisy, max_output_bytes=128)
    assert "output exceeded" in excinfo.value.message

    for script in (failing, bad_json, list_json):
        with pytest.raises(ApiError) as item:
            await run(script)
        assert item.value.code == "AI_SKILL_FAILED", script.name


@pytest.mark.asyncio
async def test_sandbox_meta_never_contains_script_body_or_arguments(tmp_path, monkeypatch):
    root = tmp_path / "skill"
    root.mkdir()
    script = _script(root, "echo.py", _ECHO)
    events = []
    monkeypatch.setattr("app.kernel.registry.active.emit_event", lambda event, payload=None: events.append((event, payload)))

    await run_isolated_process(
        script_path=script,
        arguments={"password": "super-secret-value"},
        cwd=root,
        timeout_seconds=10,
        max_output_bytes=4096,
        allowed_roots=[str(root)],
    )

    event, payload = events[-1]
    assert event == "sandbox.execution"
    assert set(payload) == {
        "outcome",
        "script",
        "duration_ms",
        "exit_code",
        "stdout_bytes",
        "stderr_bytes",
        "timed_out",
        "rlimit_enabled",
        "platform",
    }
    assert payload["outcome"] == "ok"
    assert payload["script"] == "echo.py"
    assert "super-secret-value" not in str(payload), "参数属于业务数据，不得进观测事件"


# ---------------------------------------------------------------- POSIX 加固


@posix_only
@pytest.mark.asyncio
async def test_rlimit_fsize_maps_to_resource_exceeded(tmp_path):
    """RLIMIT_FSIZE 命中（SIGXFSZ）必须归为资源超限，而不是泛化的脚本失败。"""
    root = tmp_path / "skill"
    root.mkdir()
    script = _script(
        root,
        "big_write.py",
        """
        import pathlib
        pathlib.Path("dump.bin").write_bytes(b"x" * 200000)
        print("{}")
        """,
    )

    with pytest.raises(ApiError) as excinfo:
        await run_isolated_process(
            script_path=script,
            arguments={},
            cwd=root,
            timeout_seconds=10,
            max_output_bytes=4096,
            allowed_roots=[str(root)],
            rlimit_enabled=True,
            max_file_bytes=8192,
        )

    assert excinfo.value.code == "AI_SANDBOX_RESOURCE_EXCEEDED"
    assert excinfo.value.status_code == 429


@posix_only
@pytest.mark.asyncio
async def test_timeout_kills_whole_process_group(tmp_path):
    """超时后孙进程必须随进程组一起被 killpg 清理。"""
    root = tmp_path / "skill"
    root.mkdir()
    pid_file = root / "child.pid"
    script = _script(
        root,
        "spawn.py",
        f"""
        import subprocess, time, sys, pathlib
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))
        time.sleep(60)
        """,
    )

    with pytest.raises(ApiError) as excinfo:
        await run_isolated_process(
            script_path=script, arguments={}, cwd=root, timeout_seconds=0.6, max_output_bytes=4096, allowed_roots=[str(root)]
        )
    assert excinfo.value.code == "AI_SKILL_TIMEOUT"

    child_pid = int(pid_file.read_text().strip())
    import time

    for _ in range(50):
        if not _process_alive(child_pid):
            return
        # 轮询的是外部子进程退出（与本事件循环无关），阻塞 sleep 不会掩盖并发缺陷
        time.sleep(0.1)  # noqa: ASYNC251
    pytest.fail(f"grandchild {child_pid} survived timeout: process group was not killed")


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows 平台差异")
@pytest.mark.asyncio
async def test_windows_runtime_refuses_scripts_instead_of_pretending_isolation(tmp_path):
    root = tmp_path / "skill"
    root.mkdir()
    runtime = MdSkillRuntime(root, enabled=True, sandbox_driver=ProcessSandboxDriver())
    manifest = MdSkillManifest(
        name="demo",
        description="d",
        implementation="md_script",
        declared_version=1,
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"},
        allow_scripts=True,
        required_permissions=[],
        instructions="",
        script_files=["main.py"],
        file_fingerprint="f",
        relative_path="demo",
    )

    with pytest.raises(ApiError) as excinfo:
        await runtime.execute(manifest, {})

    assert excinfo.value.code == "AI_CAPABILITY_UNAVAILABLE", "Windows 无强隔离时必须 fail closed"


# ---------------------------------------------------------------- 装配期策略


def test_plugin_roots_three_state_semantics(tmp_path):
    md_dir = tmp_path / "md"
    explicit = tmp_path / "one"
    assert SandboxDriverPlugin._resolve_roots(SimpleNamespace(sandbox_allowed_roots=None, md_skills_dir=str(md_dir))) == [str(md_dir)]
    assert SandboxDriverPlugin._resolve_roots(
        SimpleNamespace(sandbox_allowed_roots=[str(explicit), "  "], md_skills_dir=str(md_dir))
    ) == [str(explicit)]
    assert SandboxDriverPlugin._resolve_roots(SimpleNamespace(sandbox_allowed_roots=[], md_skills_dir=str(md_dir))) == []
    # settings 已解析结果优先（装配根只算一次，插件不再二次推导）
    assert SandboxDriverPlugin._resolve_roots(
        SimpleNamespace(resolved_sandbox_roots=(str(explicit),), sandbox_allowed_roots=None, md_skills_dir=str(md_dir))
    ) == [str(explicit)]


def test_empty_whitelist_refuses_assembly_instead_of_unlimited(tmp_path):
    sdk = FakeSdk(settings=SimpleNamespace(sandbox_allowed_roots=[], md_skills_dir=str(tmp_path), sandbox_rlimit_enabled=True))

    with pytest.raises(AssemblyError):
        SandboxDriverPlugin().contribute(sdk)


def test_plugin_registers_normalized_driver(tmp_path):
    root = tmp_path / "md"
    root.mkdir()
    sdk = FakeSdk(
        settings=SimpleNamespace(
            sandbox_allowed_roots=[str(root)],
            sandbox_rlimit_enabled=True,
            sandbox_max_processes=32,
            sandbox_max_file_bytes=1024,
        )
    )

    SandboxDriverPlugin().contribute(sdk)

    driver = sdk.objects["sandbox.driver"]
    assert isinstance(driver, ProcessSandboxDriver)
    assert driver.allowed_roots == [str(root.resolve())]
    assert driver.rlimit_enabled is True
    assert (driver.max_processes, driver.max_file_bytes) == (32, 1024)
    assert sdk.objects["sandbox.allowed_roots"] == (str(root.resolve()),)
    assert sdk.ports_registered["sandbox.driver"] is driver
