import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response
from app.db.session import get_db
from app.kernel.audit.service import record_audit
from app.models import User
from app.security.permissions import is_admin_user, list_user_permissions
from app.security.secret_store import SecretStore

router = APIRouter(prefix="/secrets", tags=["secrets"])

CurrentUser = Annotated[User, Depends(get_current_user)]


def _secret_convert_user(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
    """明文→密文引用属配置操作（契约不变量5：API 层权限裁决点）。

    仅连接器/模型配置者可调用：持有 connector.manage 或 model.manage 任一权限，
    或为管理员。普通登录用户拒绝，防止无配置职责的账号批量生成密文引用。
    """
    perms = list_user_permissions(db, tenant_id=user.tenant_id, user_id=user.id)
    if not (perms & {"connector.manage", "model.manage"}) and not is_admin_user(db, tenant_id=user.tenant_id, user_id=user.id):
        raise ApiError("AI_FORBIDDEN", "forbidden", 403)
    return user


ConvertUser = Annotated[User, Depends(_secret_convert_user)]


class SecretConvertRequest(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


@router.post("/convert", summary="生成密钥引用")
async def convert_secret(payload: SecretConvertRequest, request: Request, user: ConvertUser, db: Session = Depends(get_db)):
    """明文密钥 → Secret URI（Fernet 密文引用）。明文不落库，仅返回加密引用。

    Phase 2：每次转换都写 metadata-only 审计（引用 hash + 长度 + 渠道），
    明文与密文都不进入审计，避免 convert 成为无痕的凭据生成后门。
    """
    value = payload.value.strip()
    if not value:
        raise ApiError("AI_PARAM_INVALID", "secret value must not be empty", 400)
    secret_ref = f"secret://{SecretStore().encrypt(value)}"
    record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="SECRET_CONVERTED",
        resource_type="SECRET",
        resource_id=None,
        summary="secret reference generated",
        detail={
            "ref_hash": hashlib.sha256(secret_ref.encode("utf-8")).hexdigest(),
            "value_length": len(value),
            "channel": "manual",
        },
    )
    db.commit()
    return success_response(request, {"secret_ref": secret_ref})
