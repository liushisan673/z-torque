"""interceptor Port 的内置实现：审计拦截链（契约 §3.5 对照表「内置审计/配额拦截链，不可关闭审计」）。

实现类 AuditExecutionInterceptor 在内核 app/kernel/audit/service.py（审计事件流
写入属内核不变量4）；本插件仅做装配登记，并把实例经装配上下文共享给
skill_executors（SkillGateway 执行侧审计发射点）。
"""

from app.builtin_plugins.manifest import PluginManifest
from app.kernel.audit.service import AuditExecutionInterceptor


class InterceptorPlugin:
    manifest = PluginManifest(
        name="interceptor",
        description="执行侧审计拦截链（委托内核 record_audit；配额/熔断可扩展）",
        provides=("interceptor",),
    )

    def contribute(self, sdk) -> None:
        interceptor = AuditExecutionInterceptor()
        sdk.publish("interceptor", interceptor)
        sdk.register_port("interceptor", lambda: interceptor, label="builtin_audit_interceptor")
