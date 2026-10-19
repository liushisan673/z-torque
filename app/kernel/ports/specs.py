"""21 个 Port 的契约描述（阶段00 §3.5 与社区版 Port 实现对照表）。

本文件是 Port 的唯一权威描述源：名称、四族分组、社区版必需性、默认实现方式。
PortRegistry 按本表做启动校验与能力查询（/api/z-torque/capabilities）。

pending 语义（阶段01 §3.11：必须区分"尚未进入实现阶段"与"已实现但当前配置不可用"）：
- pending=False：已有满足契约的社区版实现并接入注册表（当前 4 个）。
- pending=True：业务能力可能已存在于代码中，但尚未按 Port 契约收口；
  启动校验对其豁免 fail-fast，只标记 NOT_PORTED 并告警。
后续阶段每迁移一个 Port，把 pending 置 False，此后缺失即拒绝启动。
"""

from dataclasses import dataclass


GROUP_ENTRY = "entry"
GROUP_EXECUTION = "execution"
GROUP_INFORMATION = "information"
GROUP_CROSS_CUTTING = "cross_cutting"
GROUPS = (GROUP_ENTRY, GROUP_EXECUTION, GROUP_INFORMATION, GROUP_CROSS_CUTTING)


@dataclass(frozen=True)
class PortSpec:
    name: str
    group: str
    required: bool
    description: str
    community_default: str
    pending: bool = True


PORT_SPECS: tuple[PortSpec, ...] = (
    # Entry（5）
    PortSpec("identity.provider", GROUP_ENTRY, True, "身份和会话上下文", "内置本地用户/密码 + JWT"),
    PortSpec("authz.provider", GROUP_ENTRY, True, "权限执行裁决（RBAC / 资源归属）", "内置 RBAC / 资源归属检查"),
    PortSpec("secret.provider", GROUP_ENTRY, True, "密钥加密存储与解密", "本地加密存储（文件系统或 DB）"),
    PortSpec("notify.channel", GROUP_ENTRY, False, "通知渠道", "内置日志/控制台通知"),
    PortSpec("approval.provider", GROUP_ENTRY, False, "人工审批供给", "内置人工确认队列"),
    # Execution（7）
    PortSpec("model.provider", GROUP_EXECUTION, True, "模型统一调用（generate/stream）", "OpenAI 兼容 Adapter + Mock", pending=False),
    PortSpec("model.selector", GROUP_EXECUTION, True, "按 AgentVersion 绑定选择模型", "内置规则选择器"),
    PortSpec("skill.executor", GROUP_EXECUTION, True, "工具/技能统一执行、拦截与结果标准化", "内置 SkillGateway", pending=False),
    PortSpec("sandbox.driver", GROUP_EXECUTION, False, "受控脚本沙箱执行", "进程内受限执行"),
    PortSpec("connector", GROUP_EXECUTION, False, "外部业务系统接入（MCP / HTTP）", "内置 HTTP/MCP Connector", pending=False),
    PortSpec("queue.driver", GROUP_EXECUTION, True, "任务队列进出", "内置 DB Outbox + Worker", pending=False),
    PortSpec("lock.provider", GROUP_EXECUTION, True, "并发互斥", "内置 DB 乐观锁 / 行锁"),
    # Information（6）
    PortSpec("memory.working", GROUP_INFORMATION, True, "请求级上下文组装", "内置会话上下文组装"),
    PortSpec("memory.short", GROUP_INFORMATION, True, "会话级短记忆", "内置短记忆（DB）"),
    PortSpec("memory.long", GROUP_INFORMATION, False, "长期记忆", "内置简单 KV"),
    PortSpec("vector.store", GROUP_INFORMATION, False, "向量检索", "UNAVAILABLE 或轻量实现"),
    PortSpec("knowledge.provider", GROUP_INFORMATION, False, "知识库", "UNAVAILABLE"),
    PortSpec("object.store", GROUP_INFORMATION, True, "文件对象存储", "本地文件系统"),
    # Cross-cutting（3）
    PortSpec("interceptor", GROUP_CROSS_CUTTING, True, "审计/配额拦截链", "内置审计/配额拦截链"),
    PortSpec("observability.sink", GROUP_CROSS_CUTTING, True, "结构化日志与事件", "内置结构化日志 + 事件"),
    PortSpec("policy.source", GROUP_CROSS_CUTTING, True, "策略来源", "内置配置 + 代码策略"),
)

PORT_SPEC_BY_NAME: dict[str, PortSpec] = {spec.name: spec for spec in PORT_SPECS}

assert len(PORT_SPECS) == 21, "阶段00 §3.5 锁定 21 个 Port，新增必须走内核契约变更流程"
