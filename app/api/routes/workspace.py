"""工作区事务式补丁入口（Phase 0 §2.3）。

真实入口只经 SkillGateway：driver 不直接暴露给 API/服务层，scope 与审计都在网关收口。
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response
from app.db.session import get_db
from app.kernel.security_context import SecurityContext
from app.models import User
from app.schemas.patch import PatchApplyRequest

router = APIRouter(prefix="/workspace", tags=["workspace"])


@router.post("/patch", summary="事务式写入补丁（经 SkillGateway 唯一入口）")
async def apply_patch(payload: PatchApplyRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    gateway = getattr(request.app.state, "skill_gateway", None)
    if gateway is None:
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "skill gateway is not available", 503)
    ctx = SecurityContext(tenant_id=user.tenant_id, user_id=user.id, trace_id=getattr(request.state, "trace_id", None))
    result = await gateway.apply_patch(
        db, ctx,
        patches=[item.model_dump() for item in payload.patches],
        base_dir=payload.base_dir,
        task_id=payload.task_id,
    )
    db.commit()
    return success_response(request, result)
