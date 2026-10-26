"""Create the first administrator without embedding a password in source code."""
import getpass

from sqlalchemy import select

from app.config import get_settings
from app.db.session import SessionLocal
from app.models import Permission, Role, Tenant, User, role_permission, user_role
from app.security.passwords import hash_password

ALL_PERMISSIONS: list[str] = [
    # Platform & user
    "user.read",
    "user.create",
    "user.update",
    "user.delete",
    "role.read",
    "system.config",
    # Chat
    "chat.use",
    "chat.read",
    "chat.delete",
    # Agent
    "agent.read",
    "agent.create",
    "agent.update",
    "agent.publish",
    "agent.archive",
    "agent.manage_official",
    # Skill / Tool
    "skill.read",
    "skill.create",
    "skill.update",
    "skill.publish",
    "skill.manage_official",
    "tool.read",
    "tool.manage",
    # Model
    "model.read",
    "model.manage",
    # Connector
    "connector.read",
    "connector.manage",
    # Task
    "task.read",
    "task.cancel",
    "task.approve",
    # Audit
    "audit.read",
    "audit.read_all",
    # Audit export / archive（v1.3 §5.3：导出与归档是显式授权的高危动作）
    "audit.export",
    "audit.archive",
    # A2A（v1.3 §6.5：入站调用必须显式授权，普通成员默认不授予）
    "a2a.invoke",
]


USER_ROLE_PERMISSIONS: set[str] = {
    # Chat
    "chat.use",
    "chat.read",
    "chat.delete",
    # Agent
    "agent.read",
    "agent.create",
    "agent.update",
    "agent.publish",
    "agent.archive",
    # Skill / Tool
    "skill.read",
    "skill.create",
    "skill.update",
    "skill.publish",
    "tool.read",
    # Model
    "model.read",
    # Connector
    "connector.read",
    # Task
    "task.read",
    "task.cancel",
    "task.approve",
    # Audit
    "audit.read",
}


def init_admin() -> None:
    settings = get_settings()
    if settings.admin_initial_password is None:
        password = getpass.getpass("Initial admin password: ")
    elif hasattr(settings.admin_initial_password, "get_secret_value"):
        password = settings.admin_initial_password.get_secret_value()  # type: ignore[union-attr]
    else:
        password = str(settings.admin_initial_password)
    if not password:
        raise ValueError("admin password cannot be empty")

    with SessionLocal.begin() as session:
        tenant = session.scalar(select(Tenant).where(Tenant.name == "default"))
        if tenant is None:
            tenant = Tenant(name="default")
            session.add(tenant)
            session.flush()

        # Ensure permissions exist
        perm_rows = session.scalars(select(Permission).where(Permission.tenant_id == tenant.id)).all()
        perm_by_name = {p.name: p for p in perm_rows}
        for pname in ALL_PERMISSIONS:
            if pname not in perm_by_name:
                p = Permission(tenant_id=tenant.id, name=pname)
                session.add(p)
                session.flush()
                perm_by_name[pname] = p

        # Ensure system roles exist (admin/user)
        admin_role = session.scalar(select(Role).where(Role.tenant_id == tenant.id, Role.name == "admin"))
        if admin_role is None:
            admin_role = Role(tenant_id=tenant.id, name="admin")
            session.add(admin_role)
            session.flush()

        user_role_item = session.scalar(select(Role).where(Role.tenant_id == tenant.id, Role.name == "user"))
        if user_role_item is None:
            user_role_item = Role(tenant_id=tenant.id, name="user")
            session.add(user_role_item)
            session.flush()

        # Ensure role-permission links for these system roles
        existing_rp = set(
            session.execute(
                select(role_permission.c.role_id, role_permission.c.permission_id).where(
                    role_permission.c.role_id.in_([admin_role.id, user_role_item.id])
                )
            ).all()
        )

        for pname in ALL_PERMISSIONS:
            pid = perm_by_name[pname].id
            if (admin_role.id, pid) not in existing_rp:
                session.execute(role_permission.insert().values(role_id=admin_role.id, permission_id=pid))

        for pname in USER_ROLE_PERMISSIONS:
            pid = perm_by_name[pname].id
            if (user_role_item.id, pid) not in existing_rp:
                session.execute(role_permission.insert().values(role_id=user_role_item.id, permission_id=pid))

        # Ensure admin user exists
        admin = session.scalar(select(User).where(User.username == settings.admin_initial_username))
        if admin is None:
            admin = User(tenant_id=tenant.id, username=settings.admin_initial_username, password_hash=hash_password(password))
            session.add(admin)
            session.flush()
        elif admin.tenant_id != tenant.id:
            # Keep username unique semantics; do not move cross-tenant user automatically.
            raise ValueError("admin_initial_username already exists in another tenant")

        # Ensure user has admin role
        ulink = session.execute(
            select(user_role.c.user_id)
            .where(user_role.c.user_id == admin.id, user_role.c.role_id == admin_role.id)
            .limit(1)
        ).first()
        if ulink is None:
            session.execute(user_role.insert().values(user_id=admin.id, role_id=admin_role.id))


if __name__ == "__main__":
    init_admin()
