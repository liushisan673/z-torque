"""identity.provider Port 的内置实现：本地用户/密码 + JWT + AuthSession（契约 §2 不变量1）。

认证语义自 app/api/deps.py 原地迁入（行为不变，实现归属收口）：
JWT 访问令牌解码 + 用户状态 + 服务端 AuthSession 吊销校验三重防线；
企业版可经 PortRegistry 以 PLUGIN/EXPLICIT 优先级替换为 SSO/LDAP 后端。
"""

from uuid import UUID

import jwt
from sqlalchemy.orm import Session

from app.builtin_plugins.manifest import PluginManifest
from app.core.errors import ApiError
from app.kernel.ports.identity_provider import IdentityProvider
from app.kernel.registry.port_registry import PortPriority
from app.kernel.security_context import SecurityContext
from app.models import AuthSession, User
from app.security.tokens import decode_access_token


class BuiltinIdentityProvider(IdentityProvider):
    """社区版身份提供者：令牌签名 → 用户有效 → 会话未吊销，任一不满足即 401。"""

    def authenticate(self, db: Session, token: str) -> SecurityContext:
        try:
            claims = decode_access_token(token)
            user_id = claims["sub"]
        except (jwt.InvalidTokenError, KeyError):
            raise ApiError("AI_AUTH_FAILED", "authentication failed", 401)
        user = db.get(User, user_id)
        session_id = claims.get("sid")
        auth_session = db.get(AuthSession, session_id) if session_id else None
        if user is None or not user.is_active or auth_session is None or auth_session.revoked_at is not None:
            raise ApiError("AI_AUTH_FAILED", "authentication failed", 401)
        return SecurityContext(tenant_id=user.tenant_id, user_id=user.id, session_id=UUID(str(session_id)) if session_id else None)

    def load_user(self, db: Session, user_id: UUID | str) -> User | None:
        return db.get(User, user_id)


class IdentityProviderPlugin:
    manifest = PluginManifest(
        name="identity_provider",
        description="内置身份提供者：本地用户 + JWT 访问令牌 + AuthSession 会话吊销校验",
        provides=("identity.provider",),
    )

    def contribute(self, sdk) -> None:
        sdk.register_port("identity.provider", BuiltinIdentityProvider, label="builtin_local_jwt")
