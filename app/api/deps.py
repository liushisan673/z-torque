from dataclasses import replace
from datetime import datetime, timezone

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.db.session import get_db
from app.kernel.ports.identity_provider import IdentityProvider
from app.kernel.security_context import SecurityContext
from app.models import AuthSession, User
from app.security.tokens import hash_refresh_token


def _extract_token(request: Request, authorization: str | None) -> str:
    """传输层取令牌：Bearer 头优先，兼容 cookie（浏览器端 SSE 等场景）。"""
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if token is None:
        token = request.cookies.get("access_token")
    if not token:
        raise ApiError("AI_AUTH_FAILED", "authentication required", 401)
    return token


def _identity_provider(request: Request) -> IdentityProvider:
    """经 Port 注册表取 identity.provider（API 层不 import 插件包，契约 v1.1 边界表）。"""
    registry = getattr(request.app.state, "port_registry", None)
    if registry is None:
        raise ApiError("AI_INTERNAL_ERROR", "port registry is not ready", 500)
    provider = registry.try_get("identity.provider")
    if provider is None:
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "port identity.provider is not available", 503)
    return provider


def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    provider = _identity_provider(request)
    ctx = provider.authenticate(db, _extract_token(request, authorization))
    user = provider.load_user(db, ctx.user_id)
    if user is None:
        raise ApiError("AI_AUTH_FAILED", "authentication failed", 401)
    return user


def get_optional_user(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User | None:
    """可选身份：公开接口（如 /capabilities）据此决定返回明细还是脱敏视图。"""
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if token is None:
        token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        provider = _identity_provider(request)
        ctx = provider.authenticate(db, token)
        return provider.load_user(db, ctx.user_id)
    except ApiError:
        return None


def get_security_context(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> SecurityContext:
    """构造 SecurityContext 并贯穿后续执行链（契约 §2 不变量1）；trace_id 属传输层，由 API 边界补充。"""
    provider = _identity_provider(request)
    ctx = provider.authenticate(db, _extract_token(request, authorization))
    return replace(ctx, trace_id=getattr(request.state, "trace_id", None))


def get_session(db: Session, refresh_token: str) -> AuthSession:
    auth_session = db.scalar(select(AuthSession).where(AuthSession.refresh_token_hash == hash_refresh_token(refresh_token)))
    expires_at = auth_session.expires_at if auth_session else None
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if auth_session is None or auth_session.revoked_at or expires_at <= datetime.now(timezone.utc):
        raise ApiError("AI_AUTH_FAILED", "refresh token invalid", 401)
    return auth_session
