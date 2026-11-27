"""首次开通向导：桌面分发版只需要填写模型 API Key。

智能体、技能、知识库、模型标识与接入地址都已经随包内置并绑定。
这里只把用户填写的密钥写入既有 Provider，绝不改动模型配置，
避免用户输入其它模型标识后导致智能体不可用。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response
from app.db.session import get_db
from app.kernel.agent.service import bind_bundled_model_to_agents
from app.kernel.audit.service import record_audit
from app.models import ModelProvider, User
from app.security.permissions import require_permissions
from app.security.secret_store import SecretStore

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

CurrentUser = Annotated[User, Depends(get_current_user)]
ModelManageUser = Annotated[User, Depends(require_permissions(["model.manage"]))]
DbSession = Annotated[Session, Depends(get_db)]


class OnboardingRequest(BaseModel):
    """只接收 API Key；其它模型字段一律忽略。"""

    api_key: str = Field(min_length=1, max_length=4096)


def _bundled_provider(db: Session, tenant_id) -> ModelProvider | None:
    return db.scalar(
        select(ModelProvider)
        .where(ModelProvider.tenant_id == tenant_id)
        .order_by(ModelProvider.created_at)
        .limit(1)
    )


def _model_configured(db: Session, tenant_id) -> bool:
    """以数据库 Provider 是否持有密钥为准。"""
    return db.scalar(
        select(ModelProvider.id)
        .where(
            ModelProvider.tenant_id == tenant_id,
            ModelProvider.status == "ENABLED",
            ModelProvider.secret_ref.is_not(None),
            ModelProvider.secret_ref != "",
        )
        .limit(1)
    ) is not None


@router.get("/status", summary="查询模型密钥是否已完成首次配置")
async def onboarding_status(request: Request, user: CurrentUser, db: DbSession):
    configured = _model_configured(db, user.tenant_id)
    return success_response(request, {"configured": configured, "needed": not configured})


@router.post("", summary="首次配置：仅更新内置模型的 API Key")
async def run_onboarding(
    payload: OnboardingRequest,
    request: Request,
    user: ModelManageUser,
    db: DbSession,
):
    api_key = payload.api_key.strip()
    if not api_key:
        raise ApiError("AI_PARAM_INVALID", "api_key must not be empty", 400)

    provider = _bundled_provider(db, user.tenant_id)
    if provider is None:
        raise ApiError("AI_MODEL_NOT_CONFIGURED", "bundled model provider is missing", 409)

    already_configured = bool(provider.secret_ref)
    provider.secret_ref = f"secret://{SecretStore().encrypt(api_key)}"
    provider.status = "ENABLED"
    # 兼容旧数据：若智能体快照尚未绑定模型，补成随包内置的模型标识。
    bound_agents = bind_bundled_model_to_agents(db, user.tenant_id)

    record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="ONBOARDING_COMPLETED",
        resource_type="MODEL_PROVIDER",
        resource_id=provider.id,
        summary="bundled model api key configured",
        detail={"provider_id": str(provider.id), "model_unchanged": True, "agents_bound": bound_agents},
        trace_id=request.state.trace_id,
    )
    db.commit()

    return success_response(
        request,
        {
            "configured": True,
            "already_configured": already_configured,
            "provider_id": str(provider.id),
        },
    )
