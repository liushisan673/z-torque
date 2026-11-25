"""本地知识库真实入口（Phase 0 §2.1）：检索 / 引用校验 / 存量回填 / 状态。

检索 scope 由服务端按当前用户拥有的文件推导，不信任客户端传入的路径；
FTS5 不可用时三态上报 DEGRADED 并审计，不静默退回 LIKE 假装可用。
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response
from app.db.session import get_db
from app.kernel.audit.service import record_audit, record_audit_detached
from app.kernel.ports.knowledge_provider import KnowledgeChunk
from app.kernel.security_context import SecurityContext
from app.models import KnowledgeChunk as ChunkRow
from app.models import KnowledgeDoc as DocRow
from app.models import User
from app.schemas.knowledge import CitationVerifyRequest, KnowledgeSearchRequest
from app.services import knowledge_service
from app.services.citation_guard import validate_citations

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def _ctx(request: Request, user: User) -> SecurityContext:
    return SecurityContext(tenant_id=user.tenant_id, user_id=user.id, trace_id=getattr(request.state, "trace_id", None))


@router.get("/status", summary="本地检索能力三态")
async def status(request: Request, user: User = Depends(get_current_user)):
    provider = knowledge_service.get_provider(request)
    state = knowledge_service.resolve_state(provider)
    knowledge_service.audit_degradation(provider, _ctx(request, user))
    return success_response(request, {"search": state, "fts5": bool(provider is not None and getattr(provider, "fts5", False)), "citation_enforcement": True})


@router.post("/search", summary="本地知识库检索（带坐标引用）")
async def search(payload: KnowledgeSearchRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    provider = knowledge_service.get_provider(request)
    if provider is None:
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "local knowledge search is unavailable", 503)
    ctx = _ctx(request, user)
    if payload.knowledge_base_id is not None:
        allowed = knowledge_service.allowed_paths_for_knowledge_base(request, db, tenant_id=user.tenant_id, user_id=user.id, knowledge_base_id=payload.knowledge_base_id)
    else:
        allowed = knowledge_service.allowed_paths_for_user(request, db, tenant_id=user.tenant_id, user_id=user.id)
    chunks = knowledge_service.search(db, provider=provider, ctx=ctx, query=payload.query, allowed_paths=allowed, top_k=payload.top_k)
    return success_response(request, {
        "state": knowledge_service.resolve_state(provider),
        "chunks": [{"source": item.source, "content": item.content, "score": item.score, **(item.metadata or {})} for item in chunks],
    })


@router.post("/verify-answer", summary="校验回答是否带可回查引用")
async def verify_answer(payload: CitationVerifyRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    provider = knowledge_service.get_provider(request)
    if provider is None:
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "local knowledge search is unavailable", 503)
    ctx = _ctx(request, user)
    chunks: list[KnowledgeChunk] = []
    if payload.chunk_ids:
        from uuid import UUID

        from sqlalchemy import select

        ids = []
        for raw in payload.chunk_ids:
            try:
                ids.append(UUID(raw))
            except ValueError:
                continue
        if ids:
            rows = db.execute(
                select(ChunkRow, DocRow.path).join(DocRow, ChunkRow.doc_id == DocRow.id)
                .where(ChunkRow.id.in_(ids), ChunkRow.tenant_id == user.tenant_id)
            ).all()
            chunks = [
                KnowledgeChunk(source=path, content=row.text, metadata={"chunk_id": str(row.id), "page_no": row.page_no, "sheet": row.sheet, "row_no": row.row_no, "col_no": row.col_no})
                for row, path in rows
            ]
    result = validate_citations(payload.answer, chunks)
    if not result["citation_valid"]:
        record_audit_detached(
            tenant_id=user.tenant_id, actor_user_id=user.id, action="KNOWLEDGE_CITATION_MISSING",
            resource_type="KNOWLEDGE", resource_id=None, summary="answer used retrieval without citations",
            detail={"chunk_ids": payload.chunk_ids}, trace_id=ctx.trace_id,
        )
    return success_response(request, result)


@router.post("/reindex", summary="存量文件回填索引")
async def reindex(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    provider = knowledge_service.get_provider(request)
    if provider is None:
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "local knowledge search is unavailable", 503)
    result = provider.reindex(db, tenant_id=user.tenant_id)
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="KNOWLEDGE_INDEXED",
                 resource_type="KNOWLEDGE", resource_id=None, summary="knowledge reindex",
                 detail={"indexed": result["indexed"], "state": result["mode"]}, trace_id=getattr(request.state, "trace_id", None))
    db.commit()
    return success_response(request, result)
