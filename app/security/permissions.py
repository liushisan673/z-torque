from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.db.session import get_db
from app.models import Permission, Role, User, role_permission, user_role


def list_user_permissions(db: Session, *, tenant_id: UUID, user_id: UUID) -> set[str]:
    rows = db.execute(
        select(Permission.name)
        .select_from(user_role)
        .join(Role, Role.id == user_role.c.role_id)
        .join(role_permission, role_permission.c.role_id == Role.id)
        .join(Permission, Permission.id == role_permission.c.permission_id)
        .where(Role.tenant_id == tenant_id, Permission.tenant_id == tenant_id, user_role.c.user_id == user_id)
    ).all()
    return {r[0] for r in rows}


def require_permissions(required: Iterable[str]):
    required_set = set(required)

    def dep(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
        perms = list_user_permissions(db, tenant_id=user.tenant_id, user_id=user.id)
        if not required_set.issubset(perms):
            raise ApiError("AI_FORBIDDEN", "forbidden", 403)
        return user

    return dep


def admin_user_ids_subquery(tenant_id: UUID):
    """租户内拥有 admin 角色的用户 ID 子查询。"""
    return (
        select(user_role.c.user_id)
        .join(Role, Role.id == user_role.c.role_id)
        .where(Role.tenant_id == tenant_id, Role.name == "admin")
    )


def admin_owned_clause(owner_col, tenant_id: UUID):
    """平台资源可见性：管理员创建的资源对本租户所有用户可见、可复用。
    写保护仍由各写路径的归属校验（owner == 当前用户）保证，普通用户无法修改。"""
    return owner_col.in_(admin_user_ids_subquery(tenant_id))


def require_admin(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
    # Minimal admin definition for community single-tenant deployments:
    # user has 'admin' role OR 'admin.manage' permission.
    role = db.execute(
        select(Role.id)
        .select_from(user_role)
        .join(Role, Role.id == user_role.c.role_id)
        .where(Role.tenant_id == user.tenant_id, user_role.c.user_id == user.id, Role.name == "admin")
        .limit(1)
    ).first()
    if role is not None:
        return user

    perms = list_user_permissions(db, tenant_id=user.tenant_id, user_id=user.id)
    admin_like_prefixes = ("user.", "role.", "system.")
    if "admin.manage" in perms or any(p.startswith(admin_like_prefixes) for p in perms):
        return user

    raise ApiError("AI_FORBIDDEN", "forbidden", 403)
