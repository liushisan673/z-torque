"""单插件可移植性（阶段 2/3 验收）：桩内核 + 单插件。

判定标准（可在 CI 机器执行的部分）：
1. `import` 某个标准插件**不得**拉起其他内置插件 —— 否则"整目录拷走"名存实亡；
2. 不得拉起 `app.services` / `app.api` / `app.ws`（插件不依赖 HTTP 层与业务服务层）；
3. 不得拉起内核里的**实现模块**（`md_skill_runtime` / `audit.service` / `plugin_guard`）
   —— 这三者本该在插件侧（沙箱实现已下沉、审计写入经 sdk、guard 经 sdk 注入）。

**已知未解的内核债**（不在本测试断言内，见 memory §8）：内核 Port 协议模块
（`app.kernel.ports.*`）直接 `from app.models import ...`，因此 import 任何协议都会连带
拉起 `app.models`。这需要协议层与 ORM 解耦，属独立一期，不用"隐藏 import"的方式糊过去。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.assembly import assemble
from app.builtin_plugins.modules import resolve_profile
from app.config import get_settings
from app.db.session import engine

REPO_ROOT = Path(__file__).resolve().parents[2]

STANDARD_PLUGINS = ("sandbox_driver", "audit_export_archive", "a2a_gateway")

_ISOLATION_PROBE = """
import importlib, sys

name = {name!r}
importlib.import_module("app.builtin_plugins." + name)

forbidden_prefixes = (
    "app.services",
    "app.api",
    "app.ws",
    "app.kernel.md_skill_runtime",
    "app.kernel.audit.service",
    "app.kernel.plugin_guard",
)
leaked = sorted(m for m in sys.modules if m.startswith(forbidden_prefixes))

allowed_siblings = {{"app.builtin_plugins.manifest"}}
siblings = sorted(
    m
    for m in sys.modules
    if m.startswith("app.builtin_plugins.")
    and not m.startswith("app.builtin_plugins." + name)
    and m not in allowed_siblings
)

print("LEAKED=" + ",".join(leaked))
print("SIBLINGS=" + ",".join(siblings))
"""


@pytest.mark.parametrize("plugin_name", STANDARD_PLUGINS)
def test_plugin_import_is_isolated(plugin_name: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _ISOLATION_PROBE.format(name=plugin_name)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, f"probe failed: {completed.stderr}"
    leaked = next(line for line in completed.stdout.splitlines() if line.startswith("LEAKED="))[len("LEAKED=") :]
    siblings = next(line for line in completed.stdout.splitlines() if line.startswith("SIBLINGS="))[len("SIBLINGS=") :]
    assert leaked == "", f"{plugin_name} 反向依赖了内核实现 / 服务层 / HTTP 层：{leaked}"
    assert siblings == "", f"{plugin_name} import 时连带拉起了其他插件：{siblings}"


@pytest.mark.parametrize("profile_name", ("community", "enterprise"))
def test_profile_assembles_end_to_end(profile_name: str) -> None:
    result = assemble(get_settings(), engine, resolve_profile(profile_name))
    names = {manifest.name for manifest in result.plugins}
    assert names, f"{profile_name} profile 装配后没有任何插件"
    if profile_name == "enterprise":
        assert set(STANDARD_PLUGINS) <= names, "标准插件必须出现在 enterprise profile 装配结果里"


def test_enterprise_profile_has_single_instance_per_plugin() -> None:
    # 同名插件被装配两次会让后注册者顶掉先注册者（Port 归属/行为都变得不可预测）
    result = assemble(get_settings(), engine, resolve_profile("enterprise"))
    names = [manifest.name for manifest in result.plugins]
    assert len(names) == len(set(names)), f"duplicate plugins in assembly: {names}"
