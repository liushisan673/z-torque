import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from app.config import get_settings

ALGORITHM = "HS256"


def create_access_token(user_id: str, session_id: str, tenant_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": user_id, "sid": session_id, "tid": tenant_id, "iat": now, "exp": now + timedelta(minutes=get_settings().access_token_ttl_minutes)}
    return jwt.encode(payload, get_settings().jwt_secret_key.get_secret_value(), algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, get_settings().jwt_secret_key.get_secret_value(), algorithms=[ALGORITHM])


def create_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
