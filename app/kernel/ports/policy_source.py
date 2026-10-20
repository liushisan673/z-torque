from abc import ABC, abstractmethod
from uuid import UUID

from sqlalchemy.orm import Session


class PolicySource(ABC):
    """policy.source Port 契约：策略数据来源（契约 §3 横切族；裁决点在 authz.provider）。

    抽象"策略从哪来"：权限目录查询 + 平台级白名单（MCP / 连接器目标 / 模型）。
    社区版内置配置 + DB 角色-权限图（与内置裁决器同源），企业版可替换为外部策略源。
    """

    @abstractmethod
    def user_permissions(self, db: Session, *, user_id: UUID) -> set[str]:
        """用户全量权限名集合（与内置裁决器同一角色-权限图查询）。"""

    @abstractmethod
    def mcp_allowed_hosts(self) -> list[str]:
        """MCP 端点主机白名单。"""

    @abstractmethod
    def connector_allowed_targets(self) -> list[str]:
        """连接器目标白名单。"""

    @abstractmethod
    def model_allowed_names(self) -> list[str]:
        """model.selector 模型白名单（空 = 不限制）。"""
