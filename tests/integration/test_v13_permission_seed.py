"""v1.3 权限种子验证（§5.3 / §6.5）。

v1.3 新增三个高危权限码，必须由 `python -m app.cli`（init_admin）落到库里：
- `audit.export` / `audit.archive`：导出与归档是显式授权动作；
- `a2a.invoke`：入站 A2A 调用默认不授予普通成员。

本组用例验证：种子可重复执行、存量库能被补授权限、默认角色边界正确
（admin 全量、user 不包含任何 v1.3 高危权限）。方言无关，SQLite / PostgreSQL 均适用。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, func, select

from app.cli import ALL_PERMISSIONS, USER_ROLE_PERMISSIONS, init_admin
from app.db.session import SessionLocal
from app.models import Permission, Role, Tenant, role_permission

_V13_PERMISSIONS = {"audit.export", "audit.archive", "a2a.invoke"}
_DEFAULT_TENANT = "default"


def _default_tenant_id():
    with SessionLocal() as db:
        tenant = db.scalar(select(Tenant).where(Tenant.name == _DEFAULT_TENANT))
        assert tenant is not None, "init_admin 应先创建 default 租户"
        return tenant.id


def _role_permission_names(role_name: str) -> set[str]:
    tenant_id = _default_tenant_id()
    with SessionLocal() as db:
        role = db.scalar(select(Role).where(Role.tenant_id == tenant_id, Role.name == role_name))
        assert role is not None, f"缺少系统角色 {role_name}"
        return set(
            db.scalars(
                select(Permission.name)
                .join(role_permission, role_permission.c.permission_id == Permission.id)
                .where(role_permission.c.role_id == role.id)
            ).all()
        )


def _permission_names() -> set[str]:
    tenant_id = _default_tenant_id()
    with SessionLocal() as db:
        return set(db.scalars(select(Permission.name).where(Permission.tenant_id == tenant_id)).all())


@pytest.fixture(scope="module")
def seeded() -> None:
    init_admin()


def test_declared_permissions_include_v13_codes(seeded):
    assert _V13_PERMISSIONS <= set(ALL_PERMISSIONS), "路由要求的权限码必须在种子里声明，否则线上永远 403"
    assert _V13_PERMISSIONS <= _permission_names()


def test_admin_role_gets_v13_permissions_but_user_role_does_not(seeded):
    assert _V13_PERMISSIONS <= _role_permission_names("admin")
    assert _role_permission_names("user").isdisjoint(_V13_PERMISSIONS), (
        "导出 / 归档 / A2A 入站是高危动作，普通成员默认不得持有"
    )
    assert USER_ROLE_PERMISSIONS.isdisjoint(_V13_PERMISSIONS)


def test_seed_is_idempotent(seeded):
    tenant_id = _default_tenant_id()
    with SessionLocal() as db:
        before = (
            db.scalar(select(func.count()).select_from(Permission).where(Permission.tenant_id == tenant_id)),
            db.scalar(select(func.count()).select_from(role_permission)),
        )

    init_admin()
    init_admin()

    with SessionLocal() as db:
        after = (
            db.scalar(select(func.count()).select_from(Permission).where(Permission.tenant_id == tenant_id)),
            db.scalar(select(func.count()).select_from(role_permission)),
        )
    assert after == before, "重复执行种子不得产生重复权限或角色-权限关联"


def test_seed_backfills_permissions_for_existing_database(seeded):
    """存量库升级：管理员角色被删掉的 v1.3 授权必须被种子补回。"""
    tenant_id = _default_tenant_id()
    with SessionLocal.begin() as db:
        role = db.scalar(select(Role).where(Role.tenant_id == tenant_id, Role.name == "admin"))
        perm_ids = db.scalars(
            select(Permission.id).where(Permission.tenant_id == tenant_id, Permission.name.in_(tuple(_V13_PERMISSIONS)))
        ).all()
        db.execute(
            delete(role_permission).where(
                role_permission.c.role_id == role.id,
                role_permission.c.permission_id.in_(perm_ids),
            )
        )

    assert _role_permission_names("admin").isdisjoint(_V13_PERMISSIONS), "前置条件：授权已被清空"

    init_admin()

    assert _V13_PERMISSIONS <= _role_permission_names("admin"), "种子必须能把存量库补齐，而不是只对新库生效"


@pytest.mark.parametrize("permission", sorted(_V13_PERMISSIONS))
def test_v13_permissions_are_not_granted_to_any_other_role(seeded, permission: str):
    """除 admin 外的任何角色都不得隐式持有 v1.3 高危权限。"""
    tenant_id = _default_tenant_id()
    with SessionLocal() as db:
        holders = set(
            db.scalars(
                select(Role.name)
                .join(role_permission, role_permission.c.role_id == Role.id)
                .join(Permission, Permission.id == role_permission.c.permission_id)
                .where(Role.tenant_id == tenant_id, Permission.name == permission)
            ).all()
        )
    assert holders <= {"admin"}, f"{permission} 被意外授予 {holders - {'admin'}}"
