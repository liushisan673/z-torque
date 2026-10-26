"""沙箱容器隔离真 docker 冒烟（v1.4 §4.10）。

默认 skip，`ZTORQUE_SANDBOX_CONTAINER_LIVE=1` 打开（需要本机 docker CLI + 守护进程，
且镜像已预拉取，可用 `ZTORQUE_SANDBOX_CONTAINER_IMAGE` 覆盖；Windows 装配期即拒绝容器模式，
因此本文件在 Windows 上恒 skip）。三件事必须在真容器里才算数：
1. stdout JSON 契约：stdin 传参 → stdout 收 JSON（与进程内实现同语义）；
2. 断网（--network none）：容器内不可达外网；
3. 资源上限（--memory）：OOM 被杀 → AI_SANDBOX_RESOURCE_EXCEEDED 429。
"""

from __future__ import annotations

import os

import pytest

from app.builtin_plugins.sandbox_driver import ContainerSandboxDriver
from app.core.errors import ApiError

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("ZTORQUE_SANDBOX_CONTAINER_LIVE") != "1",
        reason="set ZTORQUE_SANDBOX_CONTAINER_LIVE=1 to run live docker sandbox tests",
    ),
    pytest.mark.skipif(os.name == "nt", reason="Windows 无容器沙箱语义（装配期 fail-closed），live 冒烟只在 POSIX 上跑"),
]

_IMAGE = os.environ.get("ZTORQUE_SANDBOX_CONTAINER_IMAGE", "python:3.11-slim")


def _driver(root) -> ContainerSandboxDriver:
    return ContainerSandboxDriver(image=_IMAGE, memory="256m", cpus=1.0, pids_limit=64, allowed_roots=[str(root)])


def _script(root, name: str, body: str):
    path = root / name
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_container_echo_roundtrip(tmp_path):
    """stdin JSON 参数 → stdout JSON 结果，与进程内实现同契约。"""
    root = tmp_path / "skill"
    root.mkdir()
    script = _script(root, "echo.py", "import json, sys\nprint(json.dumps({'echo': json.loads(sys.stdin.read() or '{}')}))")

    result = await _driver(root).run(script_path=script, arguments={"q": 1}, cwd=root, timeout_seconds=120, max_output_bytes=65536)

    assert result == {"echo": {"q": 1}}


@pytest.mark.asyncio
async def test_container_has_no_network(tmp_path):
    """--network none 必须真的断网：容器内连不出任何外部地址。"""
    root = tmp_path / "skill"
    root.mkdir()
    script = _script(
        root,
        "net.py",
        """
        import json, socket
        try:
            socket.create_connection(("example.com", 80), timeout=5)
            print(json.dumps({"network": "up"}))
        except OSError:
            print(json.dumps({"network": "down"}))
        """,
    )

    result = await _driver(root).run(script_path=script, arguments={}, cwd=root, timeout_seconds=120, max_output_bytes=65536)

    assert result["network"] == "down", "容器必须断网（--network none）"


@pytest.mark.asyncio
async def test_container_oom_maps_to_resource_exceeded(tmp_path):
    """--memory 256m 下分配 512MB → 容器 OOM（退出码 137）→ 429 资源超限。"""
    root = tmp_path / "skill"
    root.mkdir()
    script = _script(root, "oom.py", "data = bytearray(512 * 1024 * 1024)\nprint('{}')")

    with pytest.raises(ApiError) as excinfo:
        await _driver(root).run(script_path=script, arguments={}, cwd=root, timeout_seconds=120, max_output_bytes=65536)

    assert excinfo.value.code == "AI_SANDBOX_RESOURCE_EXCEEDED"
    assert excinfo.value.status_code == 429
