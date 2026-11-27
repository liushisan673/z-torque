"""运行时偏好接口：设置页可即时切换的本机开关。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.core.responses import success_response
from app.models import User
from app.security.permissions import require_permissions
from app.services.runtime_settings import get_file_change_auto_keep, set_file_change_auto_keep

router = APIRouter(prefix="/settings", tags=["settings"])

CurrentUser = Annotated[User, Depends(get_current_user)]
ConfigUser = Annotated[User, Depends(require_permissions(["system.config"]))]


class FileChangeSettingRequest(BaseModel):
    auto_keep: bool


@router.get("/runtime", summary="查询本机运行时偏好")
async def get_runtime_settings(request: Request, user: CurrentUser):
    return success_response(request, {"file_change_auto_keep": get_file_change_auto_keep()})


@router.put("/runtime/file-change-auto-keep", summary="切换文件直出模式")
async def update_file_change_auto_keep(payload: FileChangeSettingRequest, request: Request, user: ConfigUser):
    value = set_file_change_auto_keep(payload.auto_keep)
    return success_response(request, {"file_change_auto_keep": value})
