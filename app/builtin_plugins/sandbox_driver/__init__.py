"""sandbox_driver 插件（sandbox.driver Port 的实现，v1.3 §4.4 / v1.4 §4.10）。

标准插件形态：实现、策略配置、Port 登记都在插件目录内；
内核只保留 Port 抽象与调用方，**本插件整体删掉后内核仍可启动**（该能力降级为不可用）。
"""

from app.builtin_plugins.sandbox_driver.container import (
    ContainerSandboxDriver,
    run_containerized_process,
    verify_docker_available,
)
from app.builtin_plugins.sandbox_driver.plugin import SandboxDriverPlugin
from app.builtin_plugins.sandbox_driver.process import (
    ProcessSandboxDriver,
    enforce_cwd_policy,
    run_isolated_process,
)

__all__ = [
    "ContainerSandboxDriver",
    "ProcessSandboxDriver",
    "SandboxDriverPlugin",
    "enforce_cwd_policy",
    "run_containerized_process",
    "run_isolated_process",
    "verify_docker_available",
]
