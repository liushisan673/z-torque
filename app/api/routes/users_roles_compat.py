from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.responses import success_response
from app.models import User
from app.security.permissions import require_admin

# Compat endpoints matching the spec:
# - /api/z-torque/users
# - /api/z-torque/roles
# Kept admin-only and backed by existing admin IAM logic.

router = APIRouter(tags=["users", "roles"])


@router.get("/users", summary="获取用户列表")
async def list_users_compat(request: Request, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    from app.api.routes.admin_iam import list_users

    return await list_users(request=request, _=_, db=db)


@router.post("/users", summary="创建用户")
async def create_user_compat(request: Request, payload: dict, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    from app.api.routes.admin_iam import create_user

    return await create_user(request=request, payload=payload, _=_, db=db)


@router.patch("/users/{user_id}", summary="更新用户")
async def patch_user_compat(
    user_id: str, request: Request, payload: dict, _: User = Depends(require_admin), db: Session = Depends(get_db)
):
    from app.api.routes.admin_iam import patch_user

    return await patch_user(user_id=user_id, request=request, payload=payload, _=_, db=db)


@router.delete("/users/{user_id}", summary="删除用户")
async def delete_user_compat(user_id: str, request: Request, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    from app.api.routes.admin_iam import delete_user

    return await delete_user(user_id=user_id, request=request, admin=_, db=db)


@router.get("/roles", summary="获取角色列表")
async def list_roles_compat(request: Request, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    from app.api.routes.admin_iam import list_roles

    return await list_roles(request=request, db=db)


@router.get("/permissions", summary="获取权限列表")
async def list_permissions_compat(request: Request, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    # Not in the spec table but useful for admin tooling.
    from app.api.routes.admin_iam import list_permissions

    return await list_permissions(request=request, db=db)
