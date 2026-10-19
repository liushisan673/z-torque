from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response
from app.db.session import get_db
from app.models import User
from app.security.secret_store import SecretStore

router = APIRouter(prefix="/secrets", tags=["secrets"])

CurrentUser = Annotated[User, Depends(get_current_user)]


class SecretConvertRequest(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


@router.post("/convert", summary="生成密钥引用")
async def convert_secret(payload: SecretConvertRequest, request: Request, user: CurrentUser):
    """明文密钥 → Secret URI（Fernet 密文引用）。明文不落库，仅返回加密引用。"""
    value = payload.value.strip()
    if not value:
        raise ApiError("AI_PARAM_INVALID", "secret value must not be empty", 400)
    encrypted = SecretStore().encrypt(value)
    return success_response(request, {"secret_ref": f"secret://{encrypted}"})
