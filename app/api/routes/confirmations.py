from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response
from app.db.session import get_db
from app.models import Confirmation, User
from app.schemas.confirmation import ConfirmationResolveRequest, ConfirmationResponse
from app.security.permissions import is_admin_user

router = APIRouter(prefix="/confirmations", tags=["confirmations"])


def _dump(row: Confirmation, username: str | None) -> dict:
    payload = ConfirmationResponse.model_validate(row).model_dump(mode="json")
    payload["requested_by_username"] = username
    return payload


@router.get("", summary="获取确认列表")
async def list_confirmations(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # 默认拒绝（契约 §5）：只有管理员与本单发起人可见，避免同租户成员读取他人工具入参
    stmt = (
        select(Confirmation, User.username)
        .outerjoin(User, User.id == Confirmation.requested_by_user_id)
        .where(Confirmation.tenant_id == user.tenant_id)
    )
    if not is_admin_user(db, tenant_id=user.tenant_id, user_id=user.id):
        stmt = stmt.where(Confirmation.requested_by_user_id == user.id)
    rows = db.execute(stmt.order_by(Confirmation.created_at.desc())).all()
    return success_response(request, [_dump(row, username) for row, username in rows])


@router.get("/{confirmation_id}", summary="获取确认详情")
async def get_confirmation(confirmation_id: UUID, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.scalar(select(Confirmation).where(Confirmation.id == confirmation_id, Confirmation.tenant_id == user.tenant_id))
    if row is None:
        raise ApiError("AI_CONFIRMATION_NOT_FOUND", "confirmation not found", 404)
    if row.requested_by_user_id != user.id and not is_admin_user(db, tenant_id=user.tenant_id, user_id=user.id):
        raise ApiError("AI_CONFIRMATION_NOT_FOUND", "confirmation not found", 404)
    username = db.scalar(select(User.username).where(User.id == row.requested_by_user_id))
    return success_response(request, _dump(row, username))


@router.post("/{confirmation_id}/resolve", summary="处理确认请求")
async def resolve(confirmation_id: UUID, payload: ConfirmationResolveRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # 裁决经 approval.provider Port（P2 收口；PEP/幂等/自审拦截语义由实现保证）
    provider = request.app.state.port_registry.get("approval.provider")
    row = provider.resolve(db, tenant_id=user.tenant_id, confirmation_id=confirmation_id, user_id=user.id, approved=payload.approved)
    db.commit()
    db.refresh(row)
    return success_response(request, ConfirmationResponse.model_validate(row).model_dump(mode="json"))
