from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.responses import success_response, trace_id_from
from app.core.servertime import iso_utc
from app.db.session import get_db
from app.kernel.audit.service import record_audit
from app.models import (
    Agent,
    AuditLog,
    Confirmation,
    Connector,
    Conversation,
    File,
    Message,
    ModelProvider,
    Permission,
    Role,
    ShortTermMemory,
    Skill,
    SkillExecutionLog,
    Task,
    Tool,
    User,
    message_file,
    role_permission,
    user_role,
)
from app.security.passwords import hash_password
from app.security.permissions import require_admin

router = APIRouter(prefix="/admin/iam", tags=["admin-iam"])

_ERR_USER_NOT_FOUND = "user not found"
_ERR_ROLE_NOT_FOUND = "role not found"
_ADMIN_ROLE_NAME = "admin"


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1)
    display_name: str | None = Field(default=None, max_length=128)
    is_active: bool = True
    role_ids: list[UUID] = Field(default_factory=list)


class UserPatchRequest(BaseModel):
    is_active: bool | None = None
    display_name: str | None = Field(default=None, max_length=128)
    role_ids: list[UUID] | None = None  # None = 不修改角色


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=1)


class SetUserRolesRequest(BaseModel):
    role_ids: list[UUID] = Field(default_factory=list)


class RoleCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class SetRolePermissionsRequest(BaseModel):
    permission_ids: list[UUID] = Field(default_factory=list)


class PermissionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)


def _user_is_active_admin(db: Session, user: User) -> bool:
    if not user.is_active:
        return False
    return bool(
        db.scalar(
            select(func.count())
            .select_from(user_role)
            .join(Role, Role.id == user_role.c.role_id)
            .where(user_role.c.user_id == user.id, Role.name == _ADMIN_ROLE_NAME, Role.tenant_id == user.tenant_id)
        )
    )


def _roles_names(db: Session, role_ids: list[UUID], tenant_id: UUID) -> set[str]:
    if not role_ids:
        return set()
    rows = db.scalars(select(Role.name).where(Role.tenant_id == tenant_id, Role.id.in_(role_ids))).all()
    return set(rows)


def _ensure_not_last_active_admin(
    db: Session, target: User, *, deactivating: bool, new_role_ids: list[UUID] | None
) -> None:
    """规则：至少保留一名可用 admin，不可禁用/移除最后一个管理员的 admin 角色。"""
    if not _user_is_active_admin(db, target):
        return
    will_lose_admin = deactivating or (
        new_role_ids is not None and _ADMIN_ROLE_NAME not in _roles_names(db, new_role_ids, target.tenant_id)
    )
    if not will_lose_admin:
        return
    active_admins = db.scalar(
        select(func.count(func.distinct(user_role.c.user_id)))
        .select_from(user_role)
        .join(Role, Role.id == user_role.c.role_id)
        .join(User, User.id == user_role.c.user_id)
        .where(Role.tenant_id == target.tenant_id, Role.name == _ADMIN_ROLE_NAME, User.is_active.is_(True))
    ) or 0
    if active_admins <= 1:
        raise ApiError("AI_PARAM_INVALID", "cannot remove or disable the last active admin", 409)


def _apply_role_ids(db: Session, admin: User, user: User, role_ids: list[UUID]) -> None:
    db.execute(delete(user_role).where(user_role.c.user_id == user.id))
    if role_ids:
        allowed = set(db.scalars(select(Role.id).where(Role.tenant_id == admin.tenant_id, Role.id.in_(role_ids))).all())
        if len(allowed) != len(set(role_ids)):
            raise ApiError("AI_PARAM_INVALID", "one or more roles not found", 422)
        for rid in role_ids:
            db.execute(user_role.insert().values(user_id=user.id, role_id=rid))


@router.get("/users", summary="获取用户列表")
async def list_users(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).where(User.tenant_id == admin.tenant_id).order_by(User.created_at.desc())).all()

    # Preload all roles/permissions for users in one pass to avoid N+1 on frontend.
    user_ids = [u.id for u in rows]
    role_rows = []
    perm_rows = []
    if user_ids:
        role_rows = db.execute(
            select(user_role.c.user_id, Role.id, Role.name)
            .select_from(user_role)
            .join(Role, Role.id == user_role.c.role_id)
            .where(user_role.c.user_id.in_(user_ids))
        ).all()
        perm_rows = db.execute(
            select(user_role.c.user_id, Permission.id, Permission.name)
            .select_from(user_role)
            .join(Role, Role.id == user_role.c.role_id)
            .join(role_permission, role_permission.c.role_id == Role.id)
            .join(Permission, Permission.id == role_permission.c.permission_id)
            .where(user_role.c.user_id.in_(user_ids))
        ).all()

    roles_by_user: dict[UUID, list[dict]] = {}
    for uid, rid, rname in role_rows:
        roles_by_user.setdefault(uid, []).append({"id": str(rid), "name": rname})

    perms_by_user: dict[UUID, list[dict]] = {}
    seen_perm: set[tuple[UUID, UUID]] = set()
    for uid, pid, pname in perm_rows:
        key = (uid, pid)
        if key in seen_perm:
            continue
        seen_perm.add(key)
        perms_by_user.setdefault(uid, []).append({"id": str(pid), "name": pname})

    for uid in perms_by_user.keys():
        perms_by_user[uid].sort(key=lambda x: x["name"])
    for uid in roles_by_user.keys():
        roles_by_user[uid].sort(key=lambda x: x["name"])
    return success_response(
        request,
        {
            "items": [
                {
                    "id": str(u.id),
                    "username": u.username,
                    "display_name": u.display_name,
                    "tenant_id": str(u.tenant_id),
                    "is_active": u.is_active,
                    "failed_login_count": u.failed_login_count,
                    "locked_until": iso_utc(u.locked_until),
                    "last_login_at": iso_utc(u.last_login_at),
                    "created_at": iso_utc(u.created_at),
                    "roles": roles_by_user.get(u.id, []),
                    "permissions": perms_by_user.get(u.id, []),
                }
                for u in rows
            ]
        },
    )


@router.post("/users", summary="创建用户")
async def create_user(payload: UserCreateRequest, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    exists = db.scalar(select(func.count(User.id)).where(User.username == payload.username)) or 0
    if exists:
        raise ApiError("AI_PARAM_INVALID", "username already exists", 409)
    user = User(
        tenant_id=admin.tenant_id,
        username=payload.username,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
        is_active=payload.is_active,
    )
    db.add(user)
    db.flush()
    _apply_role_ids(db, admin, user, payload.role_ids)
    record_audit(
        db,
        tenant_id=admin.tenant_id,
        actor_user_id=admin.id,
        action="USER_CREATED",
        resource_type="USER",
        resource_id=user.id,
        summary="user created",
        detail={"username": user.username, "role_ids": [str(r) for r in payload.role_ids]},
        trace_id=trace_id_from(request),
    )
    db.commit()
    db.refresh(user)
    return success_response(request, {"id": str(user.id)})


@router.patch("/users/{user_id}", summary="更新用户")
async def patch_user(user_id: UUID, payload: UserPatchRequest, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None or user.tenant_id != admin.tenant_id:
        raise ApiError("AI_RESOURCE_NOT_FOUND", _ERR_USER_NOT_FOUND, 404)
    # 保护：不可禁用/移除最后一个可用 admin（在落库前判定）
    _ensure_not_last_active_admin(db, user, deactivating=payload.is_active is False, new_role_ids=payload.role_ids)
    changed: list[str] = []
    if payload.is_active is not None:
        user.is_active = payload.is_active
        changed.append("is_active")
    if payload.display_name is not None:
        user.display_name = payload.display_name.strip() or None
        changed.append("display_name")
    if payload.role_ids is not None:
        _apply_role_ids(db, admin, user, payload.role_ids)
        changed.append("role_ids")
    record_audit(
        db,
        tenant_id=admin.tenant_id,
        actor_user_id=admin.id,
        action="USER_UPDATED",
        resource_type="USER",
        resource_id=user.id,
        summary="user updated",
        detail={"fields": changed, "username": user.username},
        trace_id=trace_id_from(request),
    )
    db.commit()
    return success_response(request, {"updated": True})


@router.post("/users/{user_id}/reset-password", summary="重置用户密码")
async def reset_password(user_id: UUID, payload: ResetPasswordRequest, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None or user.tenant_id != admin.tenant_id:
        raise ApiError("AI_RESOURCE_NOT_FOUND", _ERR_USER_NOT_FOUND, 404)
    user.password_hash = hash_password(payload.password)
    user.failed_login_count = 0
    user.locked_until = None
    record_audit(
        db,
        tenant_id=admin.tenant_id,
        actor_user_id=admin.id,
        action="USER_PASSWORD_RESET",
        resource_type="USER",
        resource_id=user.id,
        summary="user password reset",
        detail={"username": user.username},
        trace_id=trace_id_from(request),
    )
    db.commit()
    return success_response(request, {"updated": True})


@router.post("/users/{user_id}/unlock", summary="解锁用户")
async def unlock_user(user_id: UUID, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None or user.tenant_id != admin.tenant_id:
        raise ApiError("AI_RESOURCE_NOT_FOUND", _ERR_USER_NOT_FOUND, 404)
    user.failed_login_count = 0
    user.locked_until = None
    record_audit(
        db,
        tenant_id=admin.tenant_id,
        actor_user_id=admin.id,
        action="USER_UNLOCKED",
        resource_type="USER",
        resource_id=user.id,
        summary="user unlocked",
        detail={"username": user.username},
        trace_id=trace_id_from(request),
    )
    db.commit()
    return success_response(request, {"updated": True})


def _purge_user_data(db: Session, user_id: UUID, takeover_admin_id: UUID) -> None:
    """按依赖顺序清理用户的业务数据引用，使 zr_user 行可安全硬删除。

    - 删除：该用户的任务（含检查点/事件）、会话及消息、确认请求、文件（含消息附件关联）、记忆
    - 解除引用：Agent/Tool/Skill 归属与技能执行日志置空；审计操作者置空（审计事实保留，用户名已在摘要快照中）
    - 接管：该用户创建的连接器与模型提供方转交执行删除的管理员（列为 NOT NULL，不可置空）
    - auth_session / user_role 由外键 CASCADE 自动清理
    """
    user_convos = select(Conversation.id).where(Conversation.owner_user_id == user_id)
    user_files = select(File.id).where(File.owner_user_id == user_id)
    user_tasks = select(Task.id).where(Task.owner_user_id == user_id)

    # 1) 解除 message → task 的 RESTRICT 引用，再删任务（checkpoint/event 级联）
    db.execute(update(Message).where(Message.task_id.in_(user_tasks)).values(task_id=None))
    db.execute(delete(Task).where(Task.owner_user_id == user_id))
    # 2) 删消息（message_file 关联行随消息 CASCADE 清理），再删会话
    db.execute(delete(Message).where(Message.conversation_id.in_(user_convos)))
    db.execute(delete(Conversation).where(Conversation.owner_user_id == user_id))
    # 3) 确认请求：本人发起的删除，本人确认的置空
    db.execute(delete(Confirmation).where(Confirmation.requested_by_user_id == user_id))
    db.execute(update(Confirmation).where(Confirmation.confirmed_by_user_id == user_id).values(confirmed_by_user_id=None))
    # 4) 文件：先解除消息附件的 RESTRICT 关联行，再删文件
    db.execute(delete(message_file).where(message_file.c.file_id.in_(user_files)))
    db.execute(delete(File).where(File.owner_user_id == user_id))
    # 5) 记忆
    db.execute(delete(ShortTermMemory).where(ShortTermMemory.user_id == user_id))
    # 6) 可空归属置空（资源保留，转为公共）
    db.execute(update(Agent).where(Agent.owner_user_id == user_id).values(owner_user_id=None))
    db.execute(update(Tool).where(Tool.owner_user_id == user_id).values(owner_user_id=None))
    db.execute(update(Skill).where(Skill.owner_user_id == user_id).values(owner_user_id=None))
    db.execute(update(SkillExecutionLog).where(SkillExecutionLog.actor_user_id == user_id).values(actor_user_id=None))
    # 7) NOT NULL 创建者转交管理员
    db.execute(update(Connector).where(Connector.created_by == user_id).values(created_by=takeover_admin_id))
    db.execute(update(ModelProvider).where(ModelProvider.created_by == user_id).values(created_by=takeover_admin_id))
    # 8) 审计操作者引用置空（审计记录本身保留）
    db.execute(update(AuditLog).where(AuditLog.actor_user_id == user_id).values(actor_user_id=None))


@router.delete("/users/{user_id}", summary="删除用户")
async def delete_user(user_id: UUID, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """硬删除用户：级联清理该用户的业务数据并解除全部引用。

    规则：
    - 不可删除自己
    - 不可删除最后一个可用 admin
    - 会话/消息/文件/任务/确认/记忆随删除清除；连接器与模型提供方转交当前管理员；
      Agent/Tool/Skill 保留但归属清空；审计记录保留（操作者标记置空）
    - 完成后写 USER_DELETED 审计
    """
    user = db.get(User, user_id)
    if user is None or user.tenant_id != admin.tenant_id:
        raise ApiError("AI_RESOURCE_NOT_FOUND", _ERR_USER_NOT_FOUND, 404)
    if user.id == admin.id:
        raise ApiError("AI_PARAM_INVALID", "cannot delete your own account", 409)
    _ensure_not_last_active_admin(db, user, deactivating=True, new_role_ids=None)
    record_audit(
        db,
        tenant_id=admin.tenant_id,
        actor_user_id=admin.id,
        action="USER_DELETED",
        resource_type="USER",
        resource_id=user.id,
        summary="user deleted",
        detail={"username": user.username},
        trace_id=trace_id_from(request),
    )
    _purge_user_data(db, user.id, admin.id)
    db.delete(user)
    db.commit()
    return success_response(request, {"deleted": True})


@router.get("/roles", summary="获取角色列表")
async def list_roles(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    roles = db.scalars(select(Role).where(Role.tenant_id == admin.tenant_id).order_by(Role.created_at.desc())).all()
    items: list[dict] = []
    for role in roles:
        perm_names = db.execute(
            select(Permission.name)
            .select_from(role_permission)
            .join(Permission, Permission.id == role_permission.c.permission_id)
            .where(role_permission.c.role_id == role.id)
        ).all()
        items.append({"id": str(role.id), "name": role.name, "permissions": [p[0] for p in perm_names]})
    return success_response(request, {"items": items})


@router.post("/roles", summary="创建角色")
async def create_role(payload: RoleCreateRequest, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    exists = db.scalar(select(func.count(Role.id)).where(Role.tenant_id == admin.tenant_id, Role.name == payload.name)) or 0
    if exists:
        raise ApiError("AI_PARAM_INVALID", "role already exists", 409)
    role = Role(tenant_id=admin.tenant_id, name=payload.name)
    db.add(role)
    db.commit()
    db.refresh(role)
    return success_response(request, {"id": str(role.id)})


@router.delete("/roles/{role_id}", summary="删除角色")
async def delete_role(role_id: UUID, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    role = db.get(Role, role_id)
    if role is None or role.tenant_id != admin.tenant_id:
        raise ApiError("AI_RESOURCE_NOT_FOUND", _ERR_ROLE_NOT_FOUND, 404)
    db.execute(delete(user_role).where(user_role.c.role_id == role.id))
    db.execute(delete(role_permission).where(role_permission.c.role_id == role.id))
    db.delete(role)
    db.commit()
    return success_response(request, {"deleted": True})


@router.get("/permissions", summary="获取权限列表")
async def list_permissions(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    perms = db.scalars(select(Permission).where(Permission.tenant_id == admin.tenant_id).order_by(Permission.created_at.desc())).all()
    return success_response(request, {"items": [{"id": str(p.id), "name": p.name} for p in perms]})


@router.post("/permissions", summary="创建权限")
async def create_permission(payload: PermissionCreateRequest, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    exists = db.scalar(select(func.count(Permission.id)).where(Permission.tenant_id == admin.tenant_id, Permission.name == payload.name)) or 0
    if exists:
        raise ApiError("AI_PARAM_INVALID", "permission already exists", 409)
    perm = Permission(tenant_id=admin.tenant_id, name=payload.name)
    db.add(perm)
    db.commit()
    db.refresh(perm)
    return success_response(request, {"id": str(perm.id)})


@router.post("/roles/{role_id}/permissions", summary="设置角色权限")
async def set_role_permissions(role_id: UUID, payload: SetRolePermissionsRequest, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    role = db.get(Role, role_id)
    if role is None or role.tenant_id != admin.tenant_id:
        raise ApiError("AI_RESOURCE_NOT_FOUND", _ERR_ROLE_NOT_FOUND, 404)

    db.execute(delete(role_permission).where(role_permission.c.role_id == role.id))
    if payload.permission_ids:
        allowed = set(
            db.scalars(
                select(Permission.id).where(Permission.tenant_id == admin.tenant_id, Permission.id.in_(payload.permission_ids))
            ).all()
        )
        if len(allowed) != len(set(payload.permission_ids)):
            raise ApiError("AI_PARAM_INVALID", "one or more permissions not found", 422)
        for pid in payload.permission_ids:
            db.execute(role_permission.insert().values(role_id=role.id, permission_id=pid))
    db.commit()
    return success_response(request, {"updated": True})


@router.get("/users/{user_id}/roles", summary="获取用户角色")
async def get_user_roles(user_id: UUID, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None or user.tenant_id != admin.tenant_id:
        raise ApiError("AI_RESOURCE_NOT_FOUND", _ERR_USER_NOT_FOUND, 404)
    role_ids = db.execute(select(user_role.c.role_id).where(user_role.c.user_id == user.id)).all()
    return success_response(request, {"role_ids": [str(r[0]) for r in role_ids]})


@router.post("/users/{user_id}/roles", summary="设置用户角色")
async def set_user_roles(user_id: UUID, payload: SetUserRolesRequest, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None or user.tenant_id != admin.tenant_id:
        raise ApiError("AI_RESOURCE_NOT_FOUND", _ERR_USER_NOT_FOUND, 404)
    _ensure_not_last_active_admin(db, user, deactivating=False, new_role_ids=payload.role_ids)
    _apply_role_ids(db, admin, user, payload.role_ids)
    record_audit(
        db,
        tenant_id=admin.tenant_id,
        actor_user_id=admin.id,
        action="USER_UPDATED",
        resource_type="USER",
        resource_id=user.id,
        summary="user roles set",
        detail={"username": user.username, "role_ids": [str(r) for r in payload.role_ids]},
        trace_id=trace_id_from(request),
    )
    db.commit()
    return success_response(request, {"updated": True})
