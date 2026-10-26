"""插件测试替身（v1.3 M1~M3 专项单测共用）。

设计原则：
- 只替代"装配根职责"（Port 注册表 / SDK 会话门禁），**不替代被测逻辑**：
  会话走真实 `SessionLocal`，审计读取走真实 `iter_audit_events`，
  对象写入走真实 `LocalObjectStore`，因此断言覆盖的是生产代码路径。
- 替身方法集与合作方实际调用点严格对齐（session / read_audit /
  require_permission / registry.try_get / settings），多一个方法都不加，
  避免替身比真实 SDK 更宽容而掩盖缺权限等缺陷。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


class FakeRegistry:
    """PortRegistry 只读视图替身：按名取已解析 Port。"""

    def __init__(self, ports: dict[str, Any] | None = None) -> None:
        self._ports: dict[str, Any] = dict(ports or {})

    def try_get(self, name: str) -> Any | None:
        return self._ports.get(name)

    def get(self, name: str) -> Any:
        port = self._ports.get(name)
        if port is None:
            raise LookupError(f"port {name} is not registered in FakeRegistry")
        return port

    def put(self, name: str, value: Any) -> None:
        self._ports[name] = value


class FakeSdk:
    """PluginSdk 的最小替身：会话、审计只读缝、权限自证、装配期对象共享。"""

    def __init__(
        self,
        *,
        settings: Any = None,
        registry: FakeRegistry | None = None,
        permissions: tuple[str, ...] = ("db.query", "audit.read", "network.outbound"),
        read_audit: bool = True,
        engine: Any = None,
    ) -> None:
        self._settings = settings
        self._registry = registry if registry is not None else FakeRegistry()
        self._permissions = tuple(permissions)
        self._read_audit_allowed = read_audit
        self._engine = engine
        self.objects: dict[str, Any] = {}
        self.ports_registered: dict[str, Any] = {}
        self.session_calls = 0

    # ---- 与 PluginSdk 对齐的只读属性 ----
    @property
    def settings(self) -> Any:
        return self._settings

    @property
    def registry(self) -> FakeRegistry:
        return self._registry

    @property
    def plugin_name(self) -> str:
        return "test_plugin"

    # ---- 受控会话（严格对齐 PluginSdk.session 语义）----
    @contextmanager
    def session(self) -> Iterator[Any]:
        from sqlalchemy.orm import Session

        from app.db.session import engine as default_engine

        if "db.query" not in self._permissions:
            raise PermissionError("plugin lacks 'db.query' permission")
        bind = self._engine if self._engine is not None else default_engine
        db = Session(bind=bind, expire_on_commit=False)
        self.session_calls += 1
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ---- 审计只读缝 ----
    def read_audit(self, query: Any) -> Iterator[Any]:
        from app.kernel.audit.reader import iter_audit_events

        if not self._read_audit_allowed:
            raise PermissionError("plugin lacks audit read capability")
        return iter_audit_events(self.session, query)

    # ---- 审计写入缝（与 PluginSdk.record_audit 同语义：独立短事务）----
    def record_audit(
        self,
        *,
        tenant_id: Any,
        action: str,
        resource_type: str,
        resource_id: Any = None,
        actor_user_id: Any = None,
        summary: str,
        detail: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> None:
        from app.kernel.audit.service import record_audit

        self.require_permission("db.query")
        with self.session() as db:
            record_audit(
                db,
                tenant_id=tenant_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                actor_user_id=actor_user_id,
                summary=summary,
                detail=detail,
                trace_id=trace_id,
            )

    # ---- 内核自有表的只读投影缝 ----
    def read_published_agents(self, *, tenant_id: Any, limit: int = 50) -> list[dict[str, Any]]:
        from app.kernel.plugin_reads import list_published_agents

        self.require_permission("db.query")
        return list_published_agents(self.session, tenant_id=tenant_id, limit=limit)

    def list_tenant_ids(self) -> list[Any]:
        from app.kernel.plugin_reads import list_tenant_ids

        self.require_permission("db.query")
        return list_tenant_ids(self.session)

    # ---- 熔断/配额（真实实现，避免替身比 SDK 更宽容）----
    @property
    def guard(self) -> Any:
        from app.kernel.plugin_guard import PluginGuardRegistry

        if "guard" not in self.objects:
            self.objects["guard"] = PluginGuardRegistry(failure_threshold=5, cooldown_seconds=30.0)
        return self.objects["guard"]

    # ---- 声明式权限 ----
    def require_permission(self, permission: str) -> None:
        if permission not in self._permissions:
            raise PermissionError(f"plugin lacks '{permission}' permission")

    # ---- 装配期对象共享 / Port 注册 ----
    def register_port(self, name: str, factory, label: str = "") -> None:
        port = factory()
        self.ports_registered[name] = port
        self._registry.put(name, port)

    def publish(self, key: str, value: Any) -> None:
        self.objects[key] = value

    def shared(self, key: str, default: Any = None) -> Any:
        return self.objects.get(key, default)


class FakeSecretProvider:
    """secret.provider 替身：按密文映射返回明文，未知密文抛错（模拟解密失败）。"""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._mapping = dict(mapping or {})
        self.decrypt_calls = 0

    def decrypt(self, ciphertext: str) -> str:
        self.decrypt_calls += 1
        if ciphertext not in self._mapping:
            raise ValueError("unknown ciphertext")
        return self._mapping[ciphertext]

    def encrypt(self, plaintext: str) -> str:
        token = f"cipher:{len(self._mapping)}"
        self._mapping[token] = plaintext
        return token


class RecordingSink:
    """observability.sink 替身：记录 (event, payload) 供断言。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, payload: dict[str, Any] | None = None) -> None:
        self.events.append((event, dict(payload or {})))
