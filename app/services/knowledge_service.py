"""本地知识库内核编排（Phase 0 §2.1）：装配取 Port、索引/检索/降级审计。

不实现检索算法（在 `app/builtin_plugins/knowledge/`），也不 import 插件包：
经 PortRegistry.try_get("knowledge.provider") 取实例，与 21 Port 冻结一致。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
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


def allowed_paths_for_knowledge_base(request, db: Session, *, tenant_id: UUID, user_id: UUID, knowledge_base_id: UUID) -> list[str]:
    """只返回指定知识库内、当前用户拥有且已解析的文件路径。"""
    from app.core.errors import ApiError
    from app.models import File, KnowledgeBase, KnowledgeBaseDocument

    base = db.scalar(select(KnowledgeBase).where(
        KnowledgeBase.id == knowledge_base_id,
        KnowledgeBase.tenant_id == tenant_id,
        KnowledgeBase.owner_user_id == user_id,
        KnowledgeBase.status == "ACTIVE",
    ))
    if base is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "knowledge base not found", 404)
    registry = getattr(request.app.state, "port_registry", None)
    object_store = registry.get("object.store") if registry is not None else None
    if object_store is None:
        return []
    storage_keys = db.scalars(
        select(File.storage_key)
        .join(KnowledgeBaseDocument, KnowledgeBaseDocument.file_id == File.id)
        .where(
            KnowledgeBaseDocument.tenant_id == tenant_id,
            KnowledgeBaseDocument.knowledge_base_id == knowledge_base_id,
            File.tenant_id == tenant_id,
            File.owner_user_id == user_id,
            File.status == "PARSED",
        )
    ).all()
    return [str(Path(object_store.local_path(key)).resolve()) for key in storage_keys]


def search(
    db: Session, *, provider: KnowledgeProvider, ctx: SecurityContext,
    query: str, allowed_paths: list[str], top_k: int,
) -> list:
    """文档与消息双源检索：两类结果均有保留配额，避免文档命中占满 Top-K。"""
    audit_degradation(provider, ctx)
    limit = max(1, min(int(top_k), 50))
    documents = list(provider.search(
        db=db, tenant_id=ctx.tenant_id, query=query, top_k=limit,
        filters={"allowed_paths": allowed_paths},
    ))
    if not get_settings().knowledge_message_search_enabled:
        return documents[:limit]

    messages = list(provider.search_messages(
        db, tenant_id=ctx.tenant_id, user_id=ctx.user_id, query=query, top_k=limit,
    ))
    if messages:
        record_audit_detached(
            tenant_id=ctx.tenant_id, actor_user_id=ctx.user_id,
            action="KNOWLEDGE_MESSAGE_SEARCH", resource_type="KNOWLEDGE", resource_id=None,
            summary="message-level full-text search",
            detail={"query_hash": hashlib.sha256(query.encode("utf-8")).hexdigest(), "hits": len(messages)},
            trace_id=ctx.trace_id,
        )
    if not documents:
        return messages[:limit]
    if not messages:
        return documents[:limit]

    # 两个来源分数不可直接比较（文档为 BM25，消息为精确子串 + 时间排序），
    # 采用来源配额而不是把两种 score 混在一起排序；某一来源不足时再由另一来源补齐。
    document_quota = (limit + 1) // 2
    message_quota = limit // 2
    selected = documents[:document_quota] + messages[:message_quota]
    selected_ids = {id(item) for item in selected}
    if len(selected) < limit:
        remainder = [
            item for item in (*documents[document_quota:], *messages[message_quota:])
            if id(item) not in selected_ids
        ]
        selected.extend(remainder[:limit - len(selected)])
    return selected[:limit]


def allowed_paths_for_user(request, db: Session, *, tenant_id: UUID, user_id: UUID) -> list[str]:
    """检索 scope 绑定：只有当前用户拥有的已解析文件才进入白名单。"""
    from app.models import File

    store = getattr(request.app.state, "port_registry", None)
    object_store = store.get("object.store") if store is not None else None
    if object_store is None:
        return []
    storage_keys = db.scalars(
        select(File.storage_key).where(
            File.tenant_id == tenant_id,
            File.owner_user_id == user_id,
            File.status == "PARSED",
        )
    ).all()
    return [str(Path(object_store.local_path(key)).resolve()) for key in storage_keys]
