"""memory.long Port 的内置实现：SQLite 落库长期记忆（Phase 0 §2.2）。

替换原进程内 KV（重启即失）；保留三件套无 db 参数的兼容方法供老调用方/测试使用，
新增三级作用域、不可变键版本链、乐观锁与软删除的 scoped 方法。
企业版仍可经 PortRegistry 以 PLUGIN/EXPLICIT 优先级替换。

单机语义：tenant_id 保留但固定值；无 project_id 列，项目级由 AGENT 承载。
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.builtin_plugins.manifest import PluginManifest
from app.config import get_settings
from app.core.errors import ApiError
from app.kernel.audit.service import record_audit
from app.kernel.ports.memory_long import LongTermMemoryStore
from app.models import LongMemory, LongMemoryVersion


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope_from_key(key: str) -> tuple[str, UUID | None]:
    """兼容入口：按 key 前缀推导作用域（conversation:{id}:... → CONVERSATION）。"""
    prefix = "conversation:"
    if key.startswith(prefix):
        remainder = key[len(prefix):]
        conversation_id = remainder.split(":", 1)[0]
        try:
            return LongTermMemoryStore.SCOPE_CONVERSATION, UUID(conversation_id)
        except ValueError:
            pass
    return LongTermMemoryStore.SCOPE_USER, None


class InProcessKvLongMemory(LongTermMemoryStore):
    """TEST-ONLY 兼容实现：原进程内 KV，仅供单测对比降级语义使用。

    生产装配已改注册 DbLongTermMemoryStore；新 scoped 方法走契约默认实现，
    显式 AI_CAPABILITY_UNAVAILABLE，不静默假装持久化。
    """

    def __init__(self) -> None:
        self._items: dict[tuple[UUID, UUID, str], str] = {}

    def remember(self, *, tenant_id: UUID, user_id: UUID, key: str, value: str) -> None:
        self._items[(tenant_id, user_id, key)] = value

    def recall(self, *, tenant_id: UUID, user_id: UUID, key: str) -> str | None:
        return self._items.get((tenant_id, user_id, key))

    def forget(self, *, tenant_id: UUID, user_id: UUID, key: str) -> None:
        self._items.pop((tenant_id, user_id, key), None)


class DbLongTermMemoryStore(LongTermMemoryStore):
    """SQLite 持久化实现：三级作用域 + immutable append-only + 版本链 + 乐观锁。"""

    def _base_query(self, db: Session, *, tenant_id: UUID, user_id: UUID, key: str, agent_id: UUID | None, scope: str):
        return select(LongMemory).where(
            LongMemory.tenant_id == tenant_id,
            LongMemory.user_id == user_id,
            LongMemory.key == key,
            LongMemory.scope == scope,
            LongMemory.status == "ACTIVE",
            LongMemory.agent_id == agent_id if agent_id is not None else LongMemory.agent_id.is_(None),
        )

    def _audit(self, db: Session, *, tenant_id: UUID, actor_user_id: UUID | None, action: str, summary: str, detail: dict[str, Any], trace_id: str | None) -> None:
        record_audit(
            db,
            tenant_id=tenant_id,
            action=action,
            resource_type="MEMORY",
            resource_id=None,
            actor_user_id=actor_user_id,
            summary=summary,
            detail=detail,
            trace_id=trace_id,
        )

    def remember_scoped(
        self, db: Session, *,
        tenant_id: UUID, user_id: UUID, key: str, value: str,
        agent_id: UUID | None = None, scope: str = LongTermMemoryStore.SCOPE_USER,
        immutable: bool = False, source_ref: str = "unspecified",
        expected_version: int | None = None, expected_hash: str | None = None,
        actor_user_id: UUID | None = None, trace_id: str | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        if not source_ref or not source_ref.strip():
            raise ApiError("AI_MEMORY_SOURCE_REQUIRED", "long memory write requires source_ref", 422)
        if len(value) > settings.memory_long_max_value_chars:
            raise ApiError("AI_MEMORY_TOO_LARGE", "long memory value exceeds hard limit", 422)

        digest = _content_hash(value)
        row = db.scalar(self._base_query(db, tenant_id=tenant_id, user_id=user_id, key=key, agent_id=agent_id, scope=scope))
        conflict = row is not None and (
            (expected_version is not None and row.version != expected_version)
            or (expected_hash is not None and row.content_hash != expected_hash)
        )
        if conflict:
            self._audit(db, tenant_id=tenant_id, actor_user_id=actor_user_id, action="MEMORY_CONFLICT", summary="long memory optimistic lock conflict", detail={"key": key, "scope": scope, "expected_version": expected_version, "expected_hash": expected_hash, "actual_version": row.version, "actual_hash": row.content_hash}, trace_id=trace_id)
            raise ApiError("AI_MEMORY_CONFLICT", "long memory was modified concurrently", 409)

        if row is None:
            count = db.scalar(select(LongMemory.id).where(LongMemory.tenant_id == tenant_id, LongMemory.status == "ACTIVE").limit(settings.memory_long_max_records + 1))
            if count is not None and db.scalar(select(LongMemory.id).where(LongMemory.tenant_id == tenant_id, LongMemory.status == "ACTIVE").limit(settings.memory_long_max_records + 1).offset(settings.memory_long_max_records)) is not None:
                raise ApiError("AI_MEMORY_QUOTA_EXCEEDED", "long memory record quota exceeded; evict or raise limit", 422)
            new_row = LongMemory(tenant_id=tenant_id, user_id=user_id, agent_id=agent_id, scope=scope, key=key, value=value, immutable=1 if immutable else 0, source_ref=source_ref, content_hash=digest, version=1, status="ACTIVE")
            db.add(new_row)
            db.flush()
            db.add(LongMemoryVersion(tenant_id=tenant_id, memory_id=new_row.id, version=0, value="", superseded_by=new_row.id))
            db.flush()
            self._audit(db, tenant_id=tenant_id, actor_user_id=actor_user_id, action="MEMORY_WRITE", summary="long memory created", detail={"memory_id": str(new_row.id), "key": key, "scope": scope, "immutable": immutable, "version": 1, "source_ref": source_ref}, trace_id=trace_id)
            return {"memory_id": str(new_row.id), "version": 1, "appended": False}

        old_version = row.version
        old_value = row.value
        next_version = old_version + 1
        if immutable or row.immutable:
            # immutable append-only：旧行 SUPERSEDED，新行承载新版本，版本链记录归属
            row.status = "SUPERSEDED"
            db.add(LongMemoryVersion(tenant_id=tenant_id, memory_id=row.id, version=old_version, value=old_value, superseded_by=None))
            db.flush()
            new_row = LongMemory(tenant_id=tenant_id, user_id=user_id, agent_id=agent_id, scope=scope, key=key, value=value, immutable=1, source_ref=source_ref, content_hash=digest, version=next_version, status="ACTIVE")
            db.add(new_row)
            db.flush()
            db.add(LongMemoryVersion(tenant_id=tenant_id, memory_id=new_row.id, version=old_version, value=old_value, superseded_by=new_row.id))
            memory_id = new_row.id
        else:
            # mutable：原地更新但必须留版本
            db.add(LongMemoryVersion(tenant_id=tenant_id, memory_id=row.id, version=old_version, value=old_value, superseded_by=row.id))
            row.value = value
            row.content_hash = digest
            row.version = next_version
            row.source_ref = source_ref
            row.immutable = 0
            memory_id = row.id
        db.flush()
        self._audit(db, tenant_id=tenant_id, actor_user_id=actor_user_id, action="MEMORY_WRITE", summary="long memory updated", detail={"memory_id": str(memory_id), "key": key, "scope": scope, "immutable": immutable, "version": next_version, "source_ref": source_ref}, trace_id=trace_id)
        return {"memory_id": str(memory_id), "version": next_version, "appended": bool(immutable or row.immutable)}

    def recall_scoped(
        self, db: Session, *,
        tenant_id: UUID, user_id: UUID, key: str,
        agent_id: UUID | None = None, scope: str = LongTermMemoryStore.SCOPE_USER,
        conversation_id: UUID | None = None,
    ) -> str | None:
        if scope == LongTermMemoryStore.SCOPE_CONVERSATION:
            if conversation_id is not None and not key.startswith(f"conversation:{conversation_id}:"):
                return None
            # 会话级边界是 key 里的 conversation_id，agent_id 可为空（兼容老写入）
            agent_clause = or_(LongMemory.agent_id.is_(None), LongMemory.agent_id == agent_id) if agent_id else LongMemory.agent_id.is_(None)
        elif scope == LongTermMemoryStore.SCOPE_AGENT:
            if agent_id is None:
                return None
            agent_clause = LongMemory.agent_id == agent_id
        else:
            agent_clause = LongMemory.agent_id.is_(None)
        row = db.scalar(
            select(LongMemory).where(
                LongMemory.tenant_id == tenant_id, LongMemory.user_id == user_id,
                LongMemory.key == key, LongMemory.scope == scope, LongMemory.status == "ACTIVE",
                agent_clause,
            )
        )
        return row.value if row else None

    def recall_bundle(
        self, db: Session, *,
        tenant_id: UUID, user_id: UUID, agent_id: UUID | None,
        conversation_id: UUID | None = None,
    ) -> dict[str, str]:
        """按作用域限量读取最近记忆，并限制总注入字符数。

        不再 SELECT 用户全部 5,000 条记忆后在 Python 过滤；
        每个作用域单独走索引查询，避免单机端在构造 prompt 时产生大对象。
        conversation_id 缺失时严格不返回任何会话级记忆，防止跨会话上下文混入。
        """
        settings = get_settings()
        per_scope_limit = settings.memory_long_context_max_items_per_scope
        remaining_chars = settings.memory_long_context_max_chars
        base = (
            select(LongMemory)
            .where(
                LongMemory.tenant_id == tenant_id,
                LongMemory.user_id == user_id,
                LongMemory.status == "ACTIVE",
            )
            .order_by(LongMemory.updated_at.desc(), LongMemory.created_at.desc())
        )
        scoped_queries = [
            (
                LongTermMemoryStore.SCOPE_AGENT,
                base.where(
                    LongMemory.scope == LongTermMemoryStore.SCOPE_AGENT,
                    LongMemory.agent_id == agent_id,
                ) if agent_id is not None else None,
            ),
            (
                LongTermMemoryStore.SCOPE_USER,
                base.where(
                    LongMemory.scope == LongTermMemoryStore.SCOPE_USER,
                    LongMemory.agent_id.is_(None),
                ),
            ),
            (
                LongTermMemoryStore.SCOPE_CONVERSATION,
                base.where(
                    LongMemory.scope == LongTermMemoryStore.SCOPE_CONVERSATION,
                    LongMemory.key.like(f"conversation:{conversation_id}:%"),
                    or_(LongMemory.agent_id.is_(None), LongMemory.agent_id == agent_id) if agent_id is not None else LongMemory.agent_id.is_(None),
                ) if conversation_id is not None else None,
            ),
        ]
        bundle: dict[str, str] = {}
        for _scope, query in scoped_queries:
            if query is None or remaining_chars <= 0:
                continue
            rows = db.scalars(query.limit(per_scope_limit)).all()
            for row in rows:
                if remaining_chars <= 0:
                    break
                value = row.value or ""
                if not value:
                    continue
                key = f"{row.scope}:{row.key}"
                if len(value) > remaining_chars:
                    bundle[key] = value[:max(0, remaining_chars - 1)] + "…"
                    remaining_chars = 0
                    break
                bundle[key] = value
                remaining_chars -= len(value)
        return bundle

    def forget_scoped(
        self, db: Session, *,
        tenant_id: UUID, user_id: UUID, key: str,
        agent_id: UUID | None = None, scope: str = LongTermMemoryStore.SCOPE_USER,
        actor_user_id: UUID | None = None, trace_id: str | None = None,
    ) -> bool:
        row = db.scalar(self._base_query(db, tenant_id=tenant_id, user_id=user_id, key=key, agent_id=agent_id, scope=scope))
        if row is None:
            return False
        row.status = "DELETED"
        db.flush()
        self._audit(db, tenant_id=tenant_id, actor_user_id=actor_user_id, action="MEMORY_DELETED", summary="long memory soft-deleted", detail={"memory_id": str(row.id), "key": key, "scope": scope}, trace_id=trace_id)
        return True

    # ---- 兼容入口：老调用方/测试无 db 参数，用独立短事务 ----
    def remember(self, *, tenant_id: UUID, user_id: UUID, key: str, value: str) -> None:
        from app.db.session import SessionLocal

        scope, _ = _scope_from_key(key)
        with SessionLocal.begin() as db:
            self.remember_scoped(db, tenant_id=tenant_id, user_id=user_id, key=key, value=value, scope=scope, source_ref="compat:kv")

    def recall(self, *, tenant_id: UUID, user_id: UUID, key: str) -> str | None:
        from app.db.session import SessionLocal

        scope, conversation_id = _scope_from_key(key)
        with SessionLocal() as db:
            return self.recall_scoped(db, tenant_id=tenant_id, user_id=user_id, key=key, scope=scope, conversation_id=conversation_id)

    def forget(self, *, tenant_id: UUID, user_id: UUID, key: str) -> None:
        from app.db.session import SessionLocal

        scope, _ = _scope_from_key(key)
        with SessionLocal.begin() as db:
            self.forget_scoped(db, tenant_id=tenant_id, user_id=user_id, key=key, scope=scope)


class LongMemoryPlugin:
    manifest = PluginManifest(
        name="long_memory",
        description="SQLite 长期记忆（三级作用域 / immutable 版本链 / 乐观锁 / 软删除）",
        provides=("memory.long",),
    )

    def contribute(self, sdk) -> None:
        sdk.register_port("memory.long", DbLongTermMemoryStore, label="builtin_db_long_memory")
