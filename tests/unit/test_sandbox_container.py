"""M4 沙箱容器隔离专项单测（v1.4 §4.10）。

覆盖面：
1. docker 参数面：--network none / --read-only / no-new-privileges / 资源上限 / 挂载与 -w；
2. 契约对齐：stdout JSON 原样返回、超时 504、退出码 137 → 429、非零/非 JSON → 500、输出超限；
3. 策略：cwd 越界与脚本越界在 spawn 前拒绝（AI_SANDBOX_DENIED）；
4. 装配：docker 探测失败 AssemblyError、Windows fail-closed、contribute 分支选择与 label；
5. 观测：sandbox.execution payload 键集合固定（isolation=container），不含脚本内容与参数。

容器执行依赖 POSIX（Windows 装配期即拒绝），docker CLI 用可注入的假二进制替代：
该假二进制把 argv 记入 FAKE_DOCKER_LOG，再按 FAKE_DOCKER_MODE 决定行为。
"""

from __future__ import annotations

import json
import os
import stat
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.builtin_plugins.sandbox_driver import (
    ContainerSandboxDriver,
    SandboxDriverPlugin,
    run_containerized_process,
)
from app.core.errors import ApiError
from app.plugin_sdk import AssemblyError
from tests.support.fakes import FakeSdk

posix_only = pytest.mark.skipif(os.name == "nt", reason="容器执行分支只能在 POSIX 上验证（Windows 装配期 fail-closed）")

_FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")

if args[:1] == ["version"]:
    if os.environ.get("FAKE_DOCKER_MODE") == "version_fail":
        sys.stderr.write("daemon unreachable")
        sys.exit(1)
    print("ok")
    sys.exit(0)

mode = os.environ.get("FAKE_DOCKER_MODE", "ok")
if mode == "timeout":
    import time
    time.sleep(30)
if mode == "exit137":
    sys.exit(137)
if mode == "exit3":
    sys.exit(3)
if mode == "badjson":
    print("not json")
    sys.exit(0)
if mode == "noisy":
    print("x" * 5000)
    sys.exit(0)
print(json.dumps({"echo": json.loads(sys.stdin.read() or "{}")}))
"""


def _script(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
    return path


def _fake_docker(tmp_path: Path, *, mode: str = "ok") -> Path:
    """生成可执行的假 docker CLI（记录 argv + 按 mode 行为）。"""
    binary = tmp_path / "fake-docker"
    binary.write_text(_FAKE_DOCKER, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["FAKE_DOCKER_MODE"] = mode
    return binary


def _driver(tmp_path: Path, fake: Path, *, root: Path, emit=None) -> ContainerSandboxDriver:
    return ContainerSandboxDriver(
        image="test-image:latest",
        memory="512m",
        cpus=1.0,
        pids_limit=128,
        allowed_roots=[str(root)],
        docker_binary=str(fake),
        emit=emit,
    )


@pytest.fixture(autouse=True)
def _fake_docker_env(tmp_path, monkeypatch):
    log = tmp_path / "docker-calls.log"
    log.touch()
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("FAKE_DOCKER_MODE", "ok")
    yield
    os.environ.pop("FAKE_DOCKER_MODE", None)


def _logged_argv(log: Path) -> list[str]:
    return json.loads(log.read_text(encoding="utf-8").splitlines()[-1])


# ---------------------------------------------------------------- 参数面与契约


@posix_only
@pytest.mark.asyncio
async def test_run_passes_expected_docker_arguments(tmp_path):
    root = tmp_path / "skill"
    root.mkdir()
    other_root = tmp_path / "other"
    other_root.mkdir()
    script = _script(root, "echo.py", "import json, sys\nprint(json.dumps({'echo': json.loads(sys.stdin.read() or '{}')}))")
    fake = _fake_docker(tmp_path)

    driver = _driver(tmp_path, fake, root=root)
    result = await driver.run(script_path=script, arguments={"q": 1}, cwd=root, timeout_seconds=10, max_output_bytes=4096)

    assert result == {"echo": {"q": 1}}
    argv = _logged_argv(tmp_path / "docker-calls.log")
    assert argv[0] == "run"
    for flag in ("--rm", "-i", "--network", "--read-only", "--security-opt", "--memory", "--cpus", "--pids-limit", "-v", "-w"):
        assert flag in argv, flag
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--memory") + 1] == "512m"
    assert argv[argv.index("--cpus") + 1] == "1.0"
    assert argv[argv.index("--pids-limit") + 1] == "128"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    # workdir 以同名路径挂载且可写；其余白名单根只读
    assert argv[argv.index("-w") + 1] == str(root)
    mounts = [argv[i + 1] for i, item in enumerate(argv) if item == "-v"]
    assert f"{other_root}:{other_root}:ro" not in mounts  # 未声明进 allowed_roots 的目录不挂
    assert f"{root}:{root}" in mounts
    assert argv[-3:] == ["python", "-I", str(script)]
    assert argv[-4] == "test-image:latest"


@posix_only
@pytest.mark.asyncio
async def test_timeout_maps_to_504_and_meta(tmp_path):
    root = tmp_path / "skill"
    root.mkdir()
    script = _script(root, "slow.py", "import time; time.sleep(30)")
    fake = _fake_docker(tmp_path, mode="timeout")
    events: list[tuple[str, dict]] = []
    driver = _driver(tmp_path, fake, root=root, emit=lambda event, payload: events.append((event, payload)))

    with pytest.raises(ApiError) as excinfo:
        await driver.run(script_path=script, arguments={}, cwd=root, timeout_seconds=0.4, max_output_bytes=4096)

    assert excinfo.value.code == "AI_SKILL_TIMEOUT"
    assert excinfo.value.status_code == 504
    assert events[-1][1]["outcome"] == "timeout"
    assert events[-1][1]["timed_out"] is True


@posix_only
@pytest.mark.asyncio
async def test_exit_137_maps_to_resource_exceeded_429(tmp_path):
    root = tmp_path / "skill"
    root.mkdir()
    script = _script(root, "oom.py", "print('boom')")
    fake = _fake_docker(tmp_path, mode="exit137")
    events: list[tuple[str, dict]] = []
    driver = _driver(tmp_path, fake, root=root, emit=lambda event, payload: events.append((event, payload)))

    with pytest.raises(ApiError) as excinfo:
        await driver.run(script_path=script, arguments={}, cwd=root, timeout_seconds=10, max_output_bytes=4096)

    assert excinfo.value.code == "AI_SANDBOX_RESOURCE_EXCEEDED"
    assert excinfo.value.status_code == 429
    assert events[-1][1]["outcome"] == "resource_exceeded"


@posix_only
@pytest.mark.asyncio
async def test_nonzero_exit_bad_json_and_output_limit_map_to_skill_failed(tmp_path):
    root = tmp_path / "skill"
    root.mkdir()
    cases = (
        ("fail.py", "exit3", 4096, "failed"),
        ("bad_json.py", "badjson", 4096, "invalid_output"),
        ("noisy.py", "noisy", 128, "output_exceeded"),
    )
    for name, mode, limit, outcome in cases:
        script = _script(root, name, "print('x')")
        fake = _fake_docker(tmp_path, mode=mode)
        events: list[tuple[str, dict]] = []
        driver = _driver(tmp_path, fake, root=root, emit=lambda event, payload, _events=events: _events.append((event, payload)))

        with pytest.raises(ApiError) as excinfo:
            await driver.run(script_path=script, arguments={}, cwd=root, timeout_seconds=10, max_output_bytes=limit)

        assert excinfo.value.code == "AI_SKILL_FAILED", name
        assert excinfo.value.status_code == 500, name
        assert events[-1][1]["outcome"] == outcome, name


@posix_only
@pytest.mark.asyncio
async def test_container_meta_payload_has_fixed_keys_without_secrets(tmp_path, monkeypatch):
    root = tmp_path / "skill"
    root.mkdir()
    script = _script(root, "echo.py", "import json, sys\nprint(json.dumps({'echo': {}}))")
    fake = _fake_docker(tmp_path)
    events: list[tuple[str, dict]] = []
    driver = _driver(tmp_path, fake, root=root, emit=lambda event, payload: events.append((event, payload)))

    await driver.run(script_path=script, arguments={"password": "super-secret-value"}, cwd=root, timeout_seconds=10, max_output_bytes=4096)

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
        "isolation",
        "platform",
    }
    assert payload["isolation"] == "container"
    assert payload["outcome"] == "ok"
    assert payload["script"] == "echo.py"
    assert "super-secret-value" not in str(payload), "参数属于业务数据，不得进观测事件"


# ---------------------------------------------------------------- 策略与 fail-closed


@posix_only
@pytest.mark.asyncio
async def test_cwd_and_script_outside_roots_denied_before_spawn(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    script = _script(outside, "echo.py", "print('{}')")
    fake = _fake_docker(tmp_path)

    driver = _driver(tmp_path, fake, root=allowed)
    with pytest.raises(ApiError) as cwd_exc:
        await driver.run(script_path=script, arguments={}, cwd=outside, timeout_seconds=10, max_output_bytes=4096)
    assert cwd_exc.value.code == "AI_SANDBOX_DENIED"

    with pytest.raises(ApiError) as script_exc:
        await driver.run(script_path=script, arguments={}, cwd=allowed, timeout_seconds=10, max_output_bytes=4096)
    assert script_exc.value.code == "AI_SANDBOX_DENIED", "脚本落在白名单外 = 容器里看不到，必须 spawn 前拒绝"
    assert not (tmp_path / "docker-calls.log").read_text(encoding="utf-8").strip(), "策略拒绝不得触达 docker CLI"


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows 平台 fail-closed")
@pytest.mark.asyncio
async def test_windows_runtime_refuses_container_execution(tmp_path):
    root = tmp_path / "skill"
    root.mkdir()
    script = _script(root, "echo.py", "print('{}')")

    with pytest.raises(ApiError) as excinfo:
        await run_containerized_process(
            script_path=script,
            arguments={},
            cwd=root,
            timeout_seconds=10,
            max_output_bytes=4096,
            image="test-image:latest",
            memory="512m",
            cpus=1.0,
            pids_limit=128,
            allowed_roots=[str(root)],
            docker_binary="docker-that-must-not-be-called",
        )

    assert excinfo.value.code == "AI_SANDBOX_DENIED"


# ---------------------------------------------------------------- 装配期


def test_verify_docker_available_rejects_missing_binary(tmp_path):
    from app.builtin_plugins.sandbox_driver import verify_docker_available

    with pytest.raises(AssemblyError):
        verify_docker_available(str(tmp_path / "definitely-missing-docker"), timeout_seconds=5)


def test_contribute_rejects_unknown_isolation_mode(tmp_path):
    root = tmp_path / "md"
    root.mkdir()
    sdk = FakeSdk(settings=SimpleNamespace(sandbox_allowed_roots=[str(root)], sandbox_isolation_mode="k8s"))
    with pytest.raises(AssemblyError):
        SandboxDriverPlugin().contribute(sdk)


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows 平台 fail-closed")
def test_contribute_rejects_container_mode_on_windows(tmp_path):
    root = tmp_path / "md"
    root.mkdir()
    sdk = FakeSdk(
        settings=SimpleNamespace(
            sandbox_allowed_roots=[str(root)],
            sandbox_isolation_mode="container",
            sandbox_container_image="test-image:latest",
            sandbox_docker_binary="docker",
        )
    )
    with pytest.raises(AssemblyError):
        SandboxDriverPlugin().contribute(sdk)


@posix_only
def test_contribute_selects_container_driver_and_label(tmp_path):
    root = tmp_path / "md"
    root.mkdir()
    fake = _fake_docker(tmp_path)
    sdk = FakeSdk(
        settings=SimpleNamespace(
            sandbox_allowed_roots=[str(root)],
            sandbox_isolation_mode="container",
            sandbox_container_image="test-image:latest",
            sandbox_docker_binary=str(fake),
        )
    )

    SandboxDriverPlugin().contribute(sdk)

    driver = sdk.objects["sandbox.driver"]
    assert isinstance(driver, ContainerSandboxDriver)
    assert driver.image == "test-image:latest"
    assert driver.docker_binary == str(fake)
    assert driver.allowed_roots == [str(root.resolve())]
    assert sdk.objects["sandbox.allowed_roots"] == (str(root.resolve()),)
    assert sdk.ports_registered["sandbox.driver"] is driver


def test_contribute_process_mode_stays_default_when_settings_say_process(tmp_path):
    """默认/显式 process：构造 ProcessSandboxDriver，label 保持原语义（社区行为零变化）。"""
    root = tmp_path / "md"
    root.mkdir()
    sdk = FakeSdk(
        settings=SimpleNamespace(
            sandbox_allowed_roots=[str(root)],
            sandbox_isolation_mode="process",
            sandbox_rlimit_enabled=False,
        )
    )

    SandboxDriverPlugin().contribute(sdk)

    from app.builtin_plugins.sandbox_driver import ProcessSandboxDriver

    driver = sdk.objects["sandbox.driver"]
    assert isinstance(driver, ProcessSandboxDriver)
    assert driver.rlimit_enabled is False
