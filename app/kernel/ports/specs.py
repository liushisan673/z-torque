"""21 个 Port 的契约描述（阶段00 §3.5 与社区版 Port 实现对照表）。

本文件是 Port 的唯一权威描述源：名称、四族分组、社区版必需性、默认实现方式。
PortRegistry 按本表做启动校验与能力查询（/api/z-torque/capabilities）。

pending 语义（阶段01 §3.11：必须区分"尚未进入实现阶段"与"已实现但当前配置不可用"）：
- pending=False：已有满足契约的社区版实现并接入注册表（P2 收口后 21/21；
  vector.store / knowledge.provider 为"契约已定义、社区版无内置实现"，
  可选 Port 缺失 → capabilities 显式 UNAVAILABLE）。
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
    # 契约 §7：Port 契约版本号（新增 Port 必须定版本；破坏性变更升大版本）
    version: str = "v1"


PORT_SPECS: tuple[PortSpec, ...] = (
    # Entry（5）
    PortSpec("identity.provider", GROUP_ENTRY, True, "身份和会话上下文", "内置本地用户/密码 + JWT", pending=False),
    PortSpec("authz.provider", GROUP_ENTRY, True, "权限执行裁决（RBAC / 资源归属）", "内置 RBAC / 资源归属检查", pending=False),
    PortSpec("secret.provider", GROUP_ENTRY, True, "密钥加密存储与解密", "本地 Fernet 加密存储（SECRET_STORE_KEY）", pending=False),
    PortSpec("notify.channel", GROUP_ENTRY, False, "通知渠道", "内置日志/控制台通知", pending=False),
    PortSpec("approval.provider", GROUP_ENTRY, False, "人工审批供给", "内置人工确认队列", pending=False),
    # Execution（7）
    PortSpec("model.provider", GROUP_EXECUTION, True, "模型统一调用（generate/stream）", "OpenAI 兼容 Adapter + Mock", pending=False),
    PortSpec("model.selector", GROUP_EXECUTION, True, "按 AgentVersion 绑定选择模型", "内置规则选择器", pending=False),
    PortSpec("skill.executor", GROUP_EXECUTION, True, "工具/技能统一执行、拦截与结果标准化", "内置 SkillGateway", pending=False),
    PortSpec("sandbox.driver", GROUP_EXECUTION, False, "受控脚本沙箱执行", "进程内受限执行", pending=False),
    PortSpec("connector", GROUP_EXECUTION, False, "外部业务系统接入（MCP / HTTP）", "内置 HTTP/MCP Connector", pending=False),
    PortSpec("queue.driver", GROUP_EXECUTION, True, "任务队列进出", "进程内 InMemory 队列（Outbox 补偿恢复）", pending=False),
    PortSpec("lock.provider", GROUP_EXECUTION, True, "并发互斥", "进程内命名互斥锁（asyncio；企业版可换 DB/Redis）", pending=False),
    # Information（6）
    PortSpec("memory.working", GROUP_INFORMATION, True, "请求级上下文组装", "进程内会话工作记忆（LRU）", pending=False),
    PortSpec("memory.short", GROUP_INFORMATION, True, "会话级短记忆", "内置短记忆（DB）", pending=False),
    PortSpec("memory.long", GROUP_INFORMATION, False, "长期记忆", "内置进程内简单 KV（单机）", pending=False),
    PortSpec("vector.store", GROUP_INFORMATION, False, "向量检索", "UNAVAILABLE 或轻量实现", pending=False),
    PortSpec("knowledge.provider", GROUP_INFORMATION, False, "知识库", "UNAVAILABLE", pending=False),
    PortSpec("object.store", GROUP_INFORMATION, True, "文件对象存储", "本地文件系统", pending=False),
    # Cross-cutting（3）
    PortSpec("interceptor", GROUP_CROSS_CUTTING, True, "审计/配额拦截链", "内置审计/配额拦截链", pending=False),
    PortSpec("observability.sink", GROUP_CROSS_CUTTING, True, "结构化日志与事件", "内置结构化日志 + 事件", pending=False),
    PortSpec("policy.source", GROUP_CROSS_CUTTING, True, "策略来源", "内置配置 + 代码策略", pending=False),
)

PORT_SPEC_BY_NAME: dict[str, PortSpec] = {spec.name: spec for spec in PORT_SPECS}

assert len(PORT_SPECS) == 21, "阶段00 §3.5 锁定 21 个 Port，新增必须走内核契约变更流程"
