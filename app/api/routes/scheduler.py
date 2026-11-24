"""本机调度器受控入口（Phase 2 §四：自动化调度 / cron）。

status 登录可读；trigger 需 system.config 权限（管理员等价），避免普通用户
手动触发归档 / 清理等副作用任务。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response
from app.models import User
from app.security.permissions import require_permissions

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


def _scheduler(request: Request):
    from app.kernel.local_scheduler import LocalScheduler

    scheduler = getattr(request.app.state, "scheduler", None)
    if not isinstance(scheduler, LocalScheduler):
        raise ApiError("AI_SCHEDULE_UNAVAILABLE", "local scheduler is not assembled", 503)
    return scheduler


@router.get("/status", summary="查看本机调度任务状态")
async def scheduler_status(request: Request, user: User = Depends(get_current_user)):
    return success_response(request, _scheduler(request).status())


@router.post("/{name}/trigger", summary="手动触发调度任务")
async def scheduler_trigger(
    name: str,
    request: Request,
    user: User = Depends(require_permissions(["system.config"])),
):
    await _scheduler(request).trigger(name)
    return success_response(request, {"name": name, "triggered": True})
