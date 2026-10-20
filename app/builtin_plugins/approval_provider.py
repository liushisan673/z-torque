"""approval.provider Port 的内置实现：DB 人工确认队列（契约 §3.5 对照表「社区版最小实现」）。

审批请求/裁决语义由内核 BuiltinApprovalProvider 原地承载（审批与任务状态机的
衔接属内核不变量2）；本插件仅做装配登记。企业版可经 PortRegistry 以
PLUGIN/EXPLICIT 优先级替换为审批中心。
"""

from app.builtin_plugins.manifest import PluginManifest
from app.kernel.registry.port_registry import PortPriority
from app.kernel.skill_confirmation import BuiltinApprovalProvider


class ApprovalProviderPlugin:
    manifest = PluginManifest(
        name="approval_provider",
        description="内置人工确认队列（DB Confirmation + 自审拦截 + 幂等裁决）",
        provides=("approval.provider",),
    )

    def contribute(self, sdk) -> None:
        # 装配期对象共享（与 authz.provider 同模式）：skill_executors 在 contribute
        # 阶段即需注入审批供给，而 Port 实例化要等 resolve()。
        provider = BuiltinApprovalProvider()
        sdk.publish("approval.provider", provider)
        sdk.register_port("approval.provider", lambda: provider, label="builtin_confirmation_queue")
