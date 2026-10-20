"""SecurityContext（内核契约 §2 不变量1：身份与会话）。

只承载身份事实（租户 / 用户 / 会话 / 追踪），不含任何权限裁决逻辑
（权限属 authz.provider Port 的 PEP 职责）。

生命周期：
- API 边界：app/api/deps.get_security_context 从 JWT + AuthSession 构造；
- API → Task：create_task(ctx=...) 把身份冻结进 Task 行（tenant_id + owner_user_id）；
- Worker → Skill：TaskRuntime 从持久化 Task 重建 SecurityContext，贯穿
  AgentExecutionLoop → SkillGateway → 模型/连接器外部调用，执行期身份不漂移。
"""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class SecurityContext:
    tenant_id: UUID
    user_id: UUID | None
    session_id: UUID | None = None
    trace_id: str | None = None
