"""sandbox.driver Port 的内置实现：进程内受限执行（契约 §3.5 对照表，企业版可换 sidecar）。

执行核心 run_isolated_process 与内核默认 ProcessSandboxDriver 均在
app/kernel/md_skill_runtime.py（MdSkillRuntime 的自有执行后端，内核直构场景
语义不变）；本插件仅做装配登记。
"""

from app.builtin_plugins.manifest import PluginManifest
from app.kernel.md_skill_runtime import ProcessSandboxDriver
from app.kernel.registry.port_registry import PortPriority


class SandboxDriverPlugin:
    manifest = PluginManifest(
        name="sandbox_driver",
        description="进程内受限脚本执行（独立解释器 -I + 最小环境变量）",
        provides=("sandbox.driver",),
    )

    def contribute(self, sdk) -> None:
        # 装配期对象共享：skill_executors 构造 SkillGateway 时注入 MdSkillRuntime
        driver = ProcessSandboxDriver()
        sdk.publish("sandbox.driver", driver)
        sdk.register_port("sandbox.driver", lambda: driver, label="builtin_inprocess_sandbox")
