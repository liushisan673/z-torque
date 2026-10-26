from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import ApiError
from app.models import AuthSession, User
from app.security.passwords import verify_password
from app.security.tokens import create_access_token, create_refresh_token, hash_refresh_token


def authenticate(session: Session, username: str, password: str) -> tuple[str, str, AuthSession]:
    user = session.scalar(select(User).where(User.username == username))
    now = datetime.now(timezone.utc)
    locked_until = user.locked_until if user else None
    if locked_until and locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    if user is None or (locked_until and locked_until > now):
        raise ApiError("AI_AUTH_FAILED", "authentication failed", 401)
    if not user.is_active or not verify_password(password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= get_settings().login_failure_limit:
            user.locked_until = now + timedelta(minutes=get_settings().login_lock_minutes)
        session.commit()
        raise ApiError("AI_AUTH_FAILED", "authentication failed", 401)

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    refresh = create_refresh_token()
    auth_session = AuthSession(
        tenant_id=user.tenant_id,
        user_id=user.id,
        refresh_token_hash=hash_refresh_token(refresh),
        expires_at=now + timedelta(days=get_settings().refresh_token_ttl_days),
    )
    session.add(auth_session)
    session.commit()
    access = create_access_token(str(user.id), str(auth_session.id), str(user.tenant_id))
    return access, refresh, auth_session
