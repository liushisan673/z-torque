"""policy.source Port 的内置实现：配置 + DB 角色-权限图（契约 §3.5 对照表「内置配置 + 代码策略」）。

user_permissions 与内置裁决器（PermissionChecker）同一角色-权限图查询语义；
平台级白名单（MCP / 连接器目标 / 模型）取自配置。企业版可经 PortRegistry 以
PLUGIN/EXPLICIT 优先级替换为外部策略源（OPA 等）。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.builtin_plugins.manifest import PluginManifest
from app.config import get_settings
from app.kernel.ports.policy_source import PolicySource
from app.kernel.registry.port_registry import PortPriority
from app.models import Permission, role_permission, user_role


class ConfigPolicySource(PolicySource):
    def user_permissions(self, db: Session, *, user_id: UUID) -> set[str]:
        return set(db.scalars(
            select(Permission.name)
            .join(role_permission, role_permission.c.permission_id == Permission.id)
            .join(user_role, user_role.c.role_id == role_permission.c.role_id)
            .where(user_role.c.user_id == user_id)
        ).all())

    def mcp_allowed_hosts(self) -> list[str]:
        return list(get_settings().mcp_allowed_hosts)

    def connector_allowed_targets(self) -> list[str]:
        return list(get_settings().connector_allowed_targets)

    def model_allowed_names(self) -> list[str]:
        return list(get_settings().model_allowed_names)


class PolicySourcePlugin:
    manifest = PluginManifest(
        name="policy_source",
        description="配置 + DB 角色-权限图策略源（与内置裁决器同源，可换外部源）",
        provides=("policy.source",),
    )

    def contribute(self, sdk) -> None:
        sdk.register_port("policy.source", ConfigPolicySource, label="builtin_config_policy")
