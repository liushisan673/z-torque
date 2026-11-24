from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.responses import success_response
from app.db.session import get_db
from app.models import UsageSummary, User
from app.services.usage_service import serialize_usage_summary

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("/summary", summary="查询归一化 usage 汇总")
async def usage_summary(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    agent_id: UUID | None = Query(default=None),
    model: str | None = Query(default=None, max_length=128),
    window: str | None = Query(default=None, max_length=32),
):
    """只返回元数据；上游未报告的 token 分类返回 `unknown`，不返回 0。"""
    stmt = select(UsageSummary).where(UsageSummary.tenant_id == user.tenant_id)
    if agent_id is not None:
        stmt = stmt.where(UsageSummary.agent_id == agent_id)
    if model:
        stmt = stmt.where(UsageSummary.model == model)
    if window:
        stmt = stmt.where(UsageSummary.window == window)
    rows = db.scalars(stmt.order_by(UsageSummary.window.desc(), UsageSummary.created_at.desc())).all()
    return success_response(request, [serialize_usage_summary(row) for row in rows])
