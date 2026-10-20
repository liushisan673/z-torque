from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session


class ExecutionInterceptor(ABC):
    """interceptor Port 契约：执行侧拦截链（契约 §3 横切族）。

    网关执行路径上的统一拦截点：审计事件发射（审计不可关闭，契约红线1）；
    配额 / 插件级熔断等策略可在实现内扩展（契约 §5.5）。社区版内置审计拦截
    （委托内核 record_audit 写入路径），不改变现有审计行为。
    """

    @abstractmethod
    def on_execution(self, db: Session, *, tenant_id: UUID, actor_user_id: UUID | None, action: str, resource_type: str, resource_id: UUID | None, summary: str, detail: dict[str, Any] | None = None, trace_id: str | None = None) -> None:
        """执行事件拦截；实现应尽力而为（失败记日志），不得中断业务执行。"""
