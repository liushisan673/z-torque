"""authz.provider Port 的内置实现：包装内置 PermissionChecker（RBAC / 资源归属）。

裁决语义自 app/kernel/authz/permission_checker.py 原地复用（default-deny，
角色-权限图查询），仅以 Port 形式收口；企业版可经 PortRegistry 以
PLUGIN/EXPLICIT 优先级替换为外部策略引擎。
"""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy.orm import Session

from app.builtin_plugins.manifest import PluginManifest
from app.kernel.authz import PermissionChecker
from app.kernel.ports.authz_provider import AuthzProvider
from app.kernel.registry.port_registry import PortPriority


class BuiltinAuthzProvider(AuthzProvider):
    def __init__(self) -> None:
        self._checker = PermissionChecker()

    def has_all(self, db: Session, user_id: UUID | None, required: Iterable[str]) -> bool:
        return self._checker.has_all(db, user_id, required)


class AuthzProviderPlugin:
    manifest = PluginManifest(
        name="authz_provider",
        description="内置权限裁决：RBAC 角色-权限图检查（default-deny）",
        provides=("authz.provider",),
    )

    def contribute(self, sdk) -> None:
        # 装配期对象共享（与 secret.provider 同模式）：skill_executors 在 contribute
        # 阶段即需注入裁决器，而 Port 实例化要等 resolve()。
        provider = BuiltinAuthzProvider()
        sdk.publish("authz.provider", provider)
        sdk.register_port("authz.provider", lambda: provider, label="builtin_rbac_checker")
