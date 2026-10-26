from datetime import datetime, timezone
from uuid import UUID

from uuid6 import uuid7


def new_uuid7() -> UUID:
    return uuid7()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
