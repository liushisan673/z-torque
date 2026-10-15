from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Permission, Role, User, role_permission, user_role


class PermissionChecker:
    """Default-deny permission checker using the existing role graph."""

    def has_all(self, db: Session, user_id: UUID | None, required: Iterable[str]) -> bool:
        names = {name for name in required if name}
        if not names or user_id is None:
            return not names
        found = set(db.scalars(
            select(Permission.name)
            .join(role_permission, role_permission.c.permission_id == Permission.id)
            .join(user_role, user_role.c.role_id == role_permission.c.role_id)
            .where(user_role.c.user_id == user_id, Permission.name.in_(names))
        ).all())
        return found == names
