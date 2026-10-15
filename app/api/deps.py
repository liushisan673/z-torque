from datetime import datetime, timezone

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.db.session import get_db
from app.models import AuthSession, User
from app.security.tokens import decode_access_token
from app.security.tokens import hash_refresh_token


def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if token is None:
        token = request.cookies.get("access_token")
    if not token:
        raise ApiError("AI_AUTH_FAILED", "authentication required", 401)
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
    return user


def get_session(db: Session, refresh_token: str) -> AuthSession:
    auth_session = db.scalar(select(AuthSession).where(AuthSession.refresh_token_hash == hash_refresh_token(refresh_token)))
    expires_at = auth_session.expires_at if auth_session else None
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if auth_session is None or auth_session.revoked_at or expires_at <= datetime.now(timezone.utc):
        raise ApiError("AI_AUTH_FAILED", "refresh token invalid", 401)
    return auth_session
