from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.responses import success_response
from app.db.session import get_db
from app.models import Confirmation, User
from app.schemas.confirmation import ConfirmationResolveRequest, ConfirmationResponse

router = APIRouter(prefix="/confirmations", tags=["confirmations"])


@router.get("", summary="获取确认列表")
async def list_confirmations(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Confirmation).where(Confirmation.tenant_id == user.tenant_id).order_by(Confirmation.created_at.desc())).all()
    return success_response(request, [ConfirmationResponse.model_validate(row).model_dump(mode="json") for row in rows])


@router.get("/{confirmation_id}", summary="获取确认详情")
async def get_confirmation(confirmation_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.scalar(select(Confirmation).where(Confirmation.id == confirmation_id, Confirmation.tenant_id == user.tenant_id))
    if row is None:
        from app.core.errors import ApiError
        raise ApiError("AI_CONFIRMATION_NOT_FOUND", "confirmation not found", 404)
    return success_response(request, ConfirmationResponse.model_validate(row).model_dump(mode="json"))


@router.post("/{confirmation_id}/resolve", summary="处理确认请求")
async def resolve(confirmation_id: UUID, payload: ConfirmationResolveRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # 裁决经 approval.provider Port（P2 收口；PEP/幂等/自审拦截语义由实现保证）
    provider = request.app.state.port_registry.get("approval.provider")
    row = provider.resolve(db, tenant_id=user.tenant_id, confirmation_id=confirmation_id, user_id=user.id, approved=payload.approved)
    db.commit()
    db.refresh(row)
    return success_response(request, ConfirmationResponse.model_validate(row).model_dump(mode="json"))
