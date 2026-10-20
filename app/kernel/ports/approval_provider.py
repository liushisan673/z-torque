from abc import ABC, abstractmethod
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Confirmation


class ApprovalProvider(ABC):
    """approval.provider Port 契约：人工审批供给（契约 §3 入口族）。

    审批与任务状态机的衔接（checkpoint / WAITING_HUMAN 迁移）属内核不变量2，
    仍由内核 skill_confirmation 承载；本 Port 只抽象审批队列的创建与裁决。
    社区版内置 DB 人工确认队列（社区版最小实现），企业版可替换为审批中心。
    """

    @abstractmethod
    def request(self, db: Session, *, tenant_id: UUID, requested_by_user_id: UUID, reason: str, snapshot: dict, task_id: UUID | None = None, skill_version_id: UUID | None = None, tool_version_id: UUID | None = None) -> Confirmation:
        """创建一条 PENDING 审批并记审计（CONFIRMATION_REQUESTED）。"""

    @abstractmethod
    def resolve(self, db: Session, *, tenant_id: UUID, confirmation_id: UUID, user_id: UUID, approved: bool) -> Confirmation:
        """裁决审批（自审拦截与幂等语义由实现保证）并记审计（CONFIRMATION_RESOLVED）。"""
