from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Permission, role_permission, user_role


class PermissionChecker:
    """Default-deny permission checker using the existing role graph.

    `policy_source` 注入后，权限来源改由 policy.source Port 提供（P2-14 真实调用缝）；
    未注入时退回同名内置查询，保证单元测试与直构场景行为不变。
    """

    def __init__(self, policy_source: object | None = None) -> None:
        self._policy_source = policy_source

    def has_all(self, db: Session, user_id: UUID | None, required: Iterable[str]) -> bool:
        names = {name for name in required if name}
        if not names or user_id is None:
            return not names
        found: set[str]
        if self._policy_source is not None:
            found = set(self._policy_source.user_permissions(db, user_id=user_id))
        else:
            found = set(db.scalars(
                select(Permission.name)
                .join(role_permission, role_permission.c.permission_id == Permission.id)
                .join(user_role, user_role.c.role_id == role_permission.c.role_id)
                .where(user_role.c.user_id == user_id, Permission.name.in_(names))
            ).all())
        return found == names
