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

from collections.abc import AsyncIterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.kernel.model_gateway.contracts import ModelGateway, ModelRequest, ModelResponse, ModelUsage
from app.kernel.ports.skill_executor import ExecutionResult
from app.kernel.security_context import SecurityContext
from app.kernel.task.service import create_task
from app.models import Agent, Conversation, Message
from app.services.chat_service import ChatResult


class MockExecutor:
    """TEST-ONLY 无条件成功执行器。

    测试约定：只允许在测试局部构造的 SkillGateway 上 `register(MockExecutor())`；
    禁止回挂 `app/builtin_plugins/plugins.py` 或任何生产装配路径。
    生产 capabilities 必须不暴露该 tool_type（见 test_no_fake_success.py）。
    """

    tool_type = "MOCK"

    def execute(self, config: dict[str, Any], arguments: dict[str, Any], _timeout_seconds: float) -> ExecutionResult:
        return ExecutionResult(status="SUCCEEDED", data={"echo": arguments, "config": config})


class MockModelGateway(ModelGateway):
    """TEST-ONLY 确定性模型网关替身。

    测试约定：只允许由 `tests/conftest.py` 显式注入 `app.state.model_gateway`；
    禁止回挂生产装配（`app/builtin_plugins/plugins.py`）或生产插件模块。
    生产 capabilities 的模型实现不得出现 mock。
    """

    default_model = "mock"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        content = request.messages[-1].content if request.messages else ""
        return ModelResponse(content=f"[MOCK] {content}", finish_reason="stop", usage=ModelUsage())

    async def stream(self, request: ModelRequest) -> AsyncIterator[str]:
        response = await self.generate(request)
        yield response.content or ""


def handle_mock_chat(
    db: Session,
    *,
    ctx: SecurityContext,
    conversation_id: UUID,
    content: str,
    requires_task: bool = False,
    idempotency_key: str | None = None,
    trace_id: str | None = None,
) -> ChatResult:
    """TEST-ONLY 假聊天路径。

    测试约定：只允许测试直接调用；禁止回挂 `app/services/chat_service.py`
    或任何生产入口。生产 service 不得包含 `[MOCK]` 假回复。
    """
    tenant_id = ctx.tenant_id
    user_id = ctx.user_id
    trace_id = trace_id or ctx.trace_id
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == tenant_id,
            Conversation.owner_user_id == user_id,
            Conversation.status == "ACTIVE",
        )
    )
    if conversation is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "conversation not found", 404)
    user_message = Message(tenant_id=tenant_id, conversation_id=conversation.id, sender_type="USER", content=content, status="PROCESSING")
    db.add(user_message)
    db.flush()
    conversation.last_message_at = datetime.now(timezone.utc)
    if requires_task:
        if not idempotency_key:
            raise ApiError("AI_PARAM_INVALID", "idempotency key is required for task chat", 400)
        task = create_task(
            db, ctx=ctx,
            agent_id=conversation.agent_id,
            agent_version_id=_published_version_id(db, conversation.agent_id),
            idempotency_key=idempotency_key,
            request_summary=content,
            conversation_id=conversation.id,
            trigger_message_id=user_message.id,
            trace_id=trace_id,
        )
        user_message.status = "COMPLETED"
        return ChatResult(user_message=user_message, assistant_message=None, task_id=task.id, mocked=True)
    assistant = Message(tenant_id=tenant_id, conversation_id=conversation.id, sender_type="ASSISTANT", content="[MOCK] " + content, status="COMPLETED")
    user_message.status = "COMPLETED"
    db.add(assistant)
    db.flush()
    conversation.last_message_at = datetime.now(timezone.utc)
    return ChatResult(user_message=user_message, assistant_message=assistant, mocked=True)


def _published_version_id(db: Session, agent_id: UUID) -> UUID:
    agent = db.scalar(select(Agent).where(Agent.id == agent_id))
    if agent is None or agent.current_published_version_id is None:
        raise ApiError("AI_AGENT_VERSION_NOT_PUBLISHED", "agent has no published version", 409)
    return agent.current_published_version_id


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
