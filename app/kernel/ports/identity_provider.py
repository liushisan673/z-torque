from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.orm import Session

from app.kernel.security_context import SecurityContext

if TYPE_CHECKING:
    from app.models import User


class IdentityProvider(ABC):
    """identity.provider Port 契约：身份认证与用户加载（契约 §2 不变量1 的入口收口）。

    API 边界只经本 Port 校验请求身份并取得 SecurityContext，不感知 JWT /
    AuthSession 等具体机制；社区版内置"本地用户/密码 + JWT"实现
    （app/builtin_plugins/identity_provider.py），企业版可替换为 SSO/LDAP 后端。
    """

    @abstractmethod
    def authenticate(self, db: Session, token: str) -> SecurityContext:
        """校验访问令牌；成功返回携带身份事实的 SecurityContext（trace_id 由 API 层补充），失败抛 AI_AUTH_FAILED(401)。"""

    @abstractmethod
    def load_user(self, db: Session, user_id: UUID | str) -> User | None:
        """按 user_id 加载用户记录；不存在返回 None（调用方按未认证处理）。"""
