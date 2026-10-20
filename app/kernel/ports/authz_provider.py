from abc import ABC, abstractmethod
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy.orm import Session


class AuthzProvider(ABC):
    """authz.provider Port 契约：权限执行裁决（契约 §2.5 PEP，"裁决点位置由内核定，策略来源可换"）。

    裁决点固定在 SkillGateway 的执行前检查（PEP 位置不变），本 Port 只抽象
    策略来源：社区版内置 RBAC / 资源归属检查（app/builtin_plugins/authz_provider.py），
    企业版可替换为外部策略引擎（OPA 等）。
    """

    @abstractmethod
    def has_all(self, db: Session, user_id: UUID | None, required: Iterable[str]) -> bool:
        """裁决用户是否具备全部所需权限；default-deny（空需求视为通过）。"""
