"""本地知识库内核编排（Phase 0 §2.1）：装配取 Port、索引/检索/降级审计。

不实现检索算法（在 `app/builtin_plugins/knowledge/`），也不 import 插件包：
经 PortRegistry.try_get("knowledge.provider") 取实例，与 21 Port 冻结一致。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import get_settings
from app.kernel.audit.service import record_audit, record_audit_detached
from app.kernel.ports.knowledge_provider import KnowledgeProvider, ParsedFile
from app.kernel.security_context import SecurityContext

_DEGRADED_AUDITED: set[UUID] = set()


def get_provider(request) -> KnowledgeProvider | None:
    registry = getattr(request.app.state, "port_registry", None)
    if registry is None:
        return None
    return registry.try_get("knowledge.provider")


def resolve_state(provider: KnowledgeProvider | None) -> str:
    if provider is None:
        return "UNAVAILABLE"
    return provider.mode()


def audit_degradation(provider: KnowledgeProvider | None, ctx: SecurityContext) -> None:
    """DEGRADED 必须可见且写审计（每租户一次，避免每次检索刷屏）。"""
    if provider is None or getattr(provider, "mode", lambda: "UNAVAILABLE")() != "DEGRADED":
        return
    if ctx.tenant_id in _DEGRADED_AUDITED:
        return
    reason = getattr(provider, "last_degradation_reason", None) or "local search degraded"
    record_audit_detached(
        tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="KNOWLEDGE_DEGRADED",
        resource_type="KNOWLEDGE", resource_id=None, summary=reason,
        detail={"state": "DEGRADED", "reason": reason}, trace_id=ctx.trace_id,
    )
    _DEGRADED_AUDITED.add(ctx.tenant_id)


def index_uploaded_file(
    db: Session, *, provider: KnowledgeProvider, ctx: SecurityContext,
    file_id: UUID, path: str, sha256: str, parsed: ParsedFile, size_bytes: int,
) -> dict:
    result = provider.index_file(
        db, tenant_id=ctx.tenant_id, file_id=file_id, path=path,
        sha256=sha256, parsed=parsed, size_bytes=size_bytes,
    )
    record_audit(
        db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="KNOWLEDGE_INDEXED",
        resource_type="FILE", resource_id=file_id, summary="file indexed for local search",
        detail={"doc_id": result["doc_id"], "chunks": result["chunks"], "state": resolve_state(provider)},
        trace_id=ctx.trace_id,
    )
    if result["truncated"]:
        record_audit(
            db, tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id, action="KNOWLEDGE_TRUNCATED",
            resource_type="FILE", resource_id=file_id, summary="file chunk limits exceeded",
            detail={"chunks": len(parsed.blocks), "size_bytes": size_bytes}, trace_id=ctx.trace_id,
        )
    audit_degradation(provider, ctx)
    return result


def delete_index(db: Session, *, provider: KnowledgeProvider | None, tenant_id: UUID, file_id: UUID) -> None:
    if provider is None:
        return
    provider.delete_file(db, tenant_id=tenant_id, file_id=file_id)


def search(
    db: Session, *, provider: KnowledgeProvider, ctx: SecurityContext,
    query: str, allowed_paths: list[str], top_k: int,
) -> list:
    audit_degradation(provider, ctx)
    chunks = list(provider.search(
        db=db, tenant_id=ctx.tenant_id, query=query, top_k=top_k,
        filters={"allowed_paths": allowed_paths},
    ))
    if get_settings().knowledge_message_search_enabled:
        message_chunks = list(provider.search_messages(
            db, tenant_id=ctx.tenant_id, user_id=ctx.user_id, query=query, top_k=top_k,
        ))
        seen = {item.source for item in chunks}
        chunks.extend(item for item in message_chunks if item.source not in seen)
        if message_chunks:
            record_audit_detached(
                tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id,
                action="KNOWLEDGE_MESSAGE_SEARCH", resource_type="KNOWLEDGE", resource_id=None,
                summary="message-level full-text search",
                detail={"query_hash": hashlib.sha256(query.encode("utf-8")).hexdigest(), "hits": len(message_chunks)},
                trace_id=ctx.trace_id,
            )
    return chunks[:top_k]


def allowed_paths_for_user(request, db: Session, *, tenant_id: UUID, user_id: UUID) -> list[str]:
    """检索 scope 绑定：只有当前用户拥有的已解析文件才进入白名单。"""
    from app.models import File

    store = getattr(request.app.state, "port_registry", None)
    object_store = store.get("object.store") if store is not None else None
    rows = db.query(File).filter(File.tenant_id == tenant_id, File.owner_user_id == user_id, File.status == "PARSED").all()
    if object_store is None:
        return []
    return [str(Path(object_store.local_path(item.storage_key)).resolve()) for item in rows]
