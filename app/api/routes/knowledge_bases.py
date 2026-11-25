"""用户级知识库资源管理。

知识库只管理文件归属关系；源文件、解析结果和全文索引仍由 File/KnowledgeProvider 管理。
所有资源范围均由服务端根据当前用户派生，客户端不能提交 owner 或 tenant。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response
from app.db.session import get_db
from app.kernel.audit.service import record_audit
from app.models import File, KnowledgeBase, KnowledgeBaseDocument, KnowledgeDoc, User
from app.schemas.knowledge_base import (
    KnowledgeBaseCreate,
    KnowledgeBaseDocumentAttach,
    KnowledgeBaseDocumentResponse,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdate,
)

router = APIRouter(prefix="/knowledge/bases", tags=["knowledge"])


def _owned_base(db: Session, user: User, base_id: UUID) -> KnowledgeBase:
    item = db.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == base_id,
            KnowledgeBase.tenant_id == user.tenant_id,
            KnowledgeBase.owner_user_id == user.id,
            KnowledgeBase.status == "ACTIVE",
        )
    )
    if item is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "knowledge base not found", 404)
    return item


@router.get("", summary="列出当前用户的知识库")
async def list_bases(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(KnowledgeBase).where(
            KnowledgeBase.tenant_id == user.tenant_id,
            KnowledgeBase.owner_user_id == user.id,
            KnowledgeBase.status == "ACTIVE",
        ).order_by(KnowledgeBase.updated_at.desc())
    ).all()
    return success_response(request, [KnowledgeBaseResponse.model_validate(row).model_dump(mode="json") for row in rows])


@router.post("", summary="创建知识库", status_code=201)
async def create_base(payload: KnowledgeBaseCreate, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    name = payload.name.strip()
    if not name:
        raise ApiError("VALIDATION_ERROR", "knowledge base name cannot be blank", 422)
    item = KnowledgeBase(
        tenant_id=user.tenant_id,
        owner_user_id=user.id,
        name=name,
        description=(payload.description or "").strip() or None,
        status="ACTIVE",
    )
    db.add(item)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError("AI_RESOURCE_CONFLICT", "a knowledge base with this name already exists", 409) from exc
    record_audit(
        db, tenant_id=user.tenant_id, actor_user_id=user.id, action="KNOWLEDGE_BASE_CREATED",
        resource_type="KNOWLEDGE_BASE", resource_id=item.id, summary="knowledge base created",
    )
    db.commit()
    db.refresh(item)
    return success_response(request, KnowledgeBaseResponse.model_validate(item).model_dump(mode="json"))


@router.get("/{base_id}", summary="获取知识库详情")
async def get_base(base_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = _owned_base(db, user, base_id)
    count = db.scalar(
        select(KnowledgeBaseDocument.id).where(
            KnowledgeBaseDocument.tenant_id == user.tenant_id,
            KnowledgeBaseDocument.knowledge_base_id == item.id,
        ).limit(1)
    )
    payload = KnowledgeBaseResponse.model_validate(item).model_dump(mode="json")
    payload["has_documents"] = count is not None
    return success_response(request, payload)


@router.patch("/{base_id}", summary="更新知识库")
async def update_base(base_id: UUID, payload: KnowledgeBaseUpdate, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = _owned_base(db, user, base_id)
    values = payload.model_dump(exclude_unset=True)
    if "name" in values:
        name = (values["name"] or "").strip()
        if not name:
            raise ApiError("VALIDATION_ERROR", "knowledge base name cannot be blank", 422)
        item.name = name
    if "description" in values:
        item.description = (values["description"] or "").strip() or None
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError("AI_RESOURCE_CONFLICT", "a knowledge base with this name already exists", 409) from exc
    record_audit(
        db, tenant_id=user.tenant_id, actor_user_id=user.id, action="KNOWLEDGE_BASE_UPDATED",
        resource_type="KNOWLEDGE_BASE", resource_id=item.id, summary="knowledge base updated",
    )
    db.commit()
    db.refresh(item)
    return success_response(request, KnowledgeBaseResponse.model_validate(item).model_dump(mode="json"))


@router.delete("/{base_id}", summary="删除知识库")
async def delete_base(base_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = _owned_base(db, user, base_id)
    # 删除关联，不删除用户文件、原文、chunk 或现有全文索引。
    db.query(KnowledgeBaseDocument).filter(
        KnowledgeBaseDocument.tenant_id == user.tenant_id,
        KnowledgeBaseDocument.knowledge_base_id == item.id,
    ).delete(synchronize_session=False)
    record_audit(
        db, tenant_id=user.tenant_id, actor_user_id=user.id, action="KNOWLEDGE_BASE_DELETED",
        resource_type="KNOWLEDGE_BASE", resource_id=item.id, summary="knowledge base deleted",
    )
    db.delete(item)
    db.commit()
    return success_response(request, {"deleted": True, "id": str(base_id)})


@router.get("/{base_id}/documents", summary="列出知识库文档")
async def list_documents(base_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _owned_base(db, user, base_id)
    rows = db.execute(
        select(KnowledgeBaseDocument, File, KnowledgeDoc.id)
        .join(File, File.id == KnowledgeBaseDocument.file_id)
        .outerjoin(
            KnowledgeDoc,
            (KnowledgeDoc.file_id == File.id) & (KnowledgeDoc.tenant_id == user.tenant_id),
        )
        .where(
            KnowledgeBaseDocument.tenant_id == user.tenant_id,
            KnowledgeBaseDocument.knowledge_base_id == base_id,
            File.tenant_id == user.tenant_id,
            File.owner_user_id == user.id,
        )
        .order_by(KnowledgeBaseDocument.created_at.desc())
    ).all()
    result = []
    for link, file, indexed_id in rows:
        result.append(KnowledgeBaseDocumentResponse(
            file_id=file.id,
            file_name=file.original_name,
            media_type=file.media_type,
            size_bytes=file.size_bytes,
            file_status=file.status,
            indexed=indexed_id is not None,
            attached_at=link.created_at,
        ).model_dump(mode="json"))
    return success_response(request, result)


@router.post("/{base_id}/documents", summary="将已上传文件加入知识库", status_code=201)
async def attach_document(base_id: UUID, payload: KnowledgeBaseDocumentAttach, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = _owned_base(db, user, base_id)
    file = db.scalar(
        select(File).where(
            File.id == payload.file_id,
            File.tenant_id == user.tenant_id,
            File.owner_user_id == user.id,
        )
    )
    if file is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "file not found", 404)
    if file.status != "PARSED":
        raise ApiError("AI_REQUEST_INVALID", "only successfully parsed files can be added to a knowledge base", 409)
    indexed = db.scalar(
        select(KnowledgeDoc.id).where(
            KnowledgeDoc.tenant_id == user.tenant_id,
            KnowledgeDoc.file_id == file.id,
            KnowledgeDoc.status == "ACTIVE",
        )
    )
    if indexed is None:
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "file has not been indexed for knowledge retrieval", 409)
    existing = db.scalar(
        select(KnowledgeBaseDocument).where(
            KnowledgeBaseDocument.tenant_id == user.tenant_id,
            KnowledgeBaseDocument.knowledge_base_id == item.id,
            KnowledgeBaseDocument.file_id == file.id,
        )
    )
    if existing is None:
        link = KnowledgeBaseDocument(tenant_id=user.tenant_id, knowledge_base_id=item.id, file_id=file.id)
        db.add(link)
        record_audit(
            db, tenant_id=user.tenant_id, actor_user_id=user.id, action="KNOWLEDGE_DOCUMENT_ATTACHED",
            resource_type="FILE", resource_id=file.id, summary="file attached to knowledge base",
            detail={"knowledge_base_id": str(item.id)},
        )
        db.commit()
    return success_response(request, {
        "knowledge_base_id": str(item.id),
        "file_id": str(file.id),
        "attached": True,
        "already_attached": existing is not None,
    })


@router.delete("/{base_id}/documents/{file_id}", summary="从知识库移除文档")
async def detach_document(base_id: UUID, file_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    item = _owned_base(db, user, base_id)
    link = db.scalar(
        select(KnowledgeBaseDocument).where(
            KnowledgeBaseDocument.tenant_id == user.tenant_id,
            KnowledgeBaseDocument.knowledge_base_id == item.id,
            KnowledgeBaseDocument.file_id == file_id,
        )
    )
    if link is None:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "knowledge base document not found", 404)
    db.delete(link)
    record_audit(
        db, tenant_id=user.tenant_id, actor_user_id=user.id, action="KNOWLEDGE_DOCUMENT_DETACHED",
        resource_type="FILE", resource_id=file_id, summary="file detached from knowledge base",
        detail={"knowledge_base_id": str(item.id)},
    )
    db.commit()
    return success_response(request, {"detached": True, "file_id": str(file_id)})
