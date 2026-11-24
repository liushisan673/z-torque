"""首次开通向导：一次提交创建模型接入与可用助手。

设计目标（阶段13 §4.3）：新部署的管理员不必逐页配置供应商、模型、接入点、
智能体并手动发布，只需填写厂商、API Key、模型标识与助手名即可直接开始对话。
整条链路在单个事务内完成，任一步失败整体回滚；重复提交按助手名幂等返回。
"""

from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.responses import success_response
from app.db.session import get_db
from app.kernel.agent.service import create_agent
from app.kernel.agent.version_service import create_version, publish_version
from app.kernel.audit.service import record_audit
from app.models import Agent, Conversation, ModelEndpoint, ModelProvider, ModelResource, User
from app.security.permissions import require_permissions
from app.security.secret_store import SecretStore

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

ModelManageUser = Annotated[User, Depends(require_permissions(["model.manage"]))]
DbSession = Annotated[Session, Depends(get_db)]

_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTS = {"169.254.169.254", "metadata.google.internal", "metadata", "fd00:ec2::254"}
_INTERFACE_TYPES = {"openai_compatible", "anthropic", "custom"}
_DEFAULT_SYSTEM_PROMPT = "你是一个乐于助人的助手，请用简洁清晰的语言回答用户问题。"


class OnboardingRequest(BaseModel):
    """开通向导单屏表单：用户只需理解厂商、密钥、模型标识与助手名。"""

    vendor: str = Field(min_length=1, max_length=64)
    api_key: str = Field(min_length=1, max_length=4096)
    model_id: str = Field(min_length=1, max_length=128)
    assistant_name: str = Field(default="我的助手", min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=512)
    interface_type: str = Field(default="openai_compatible")
    system_prompt: str | None = Field(default=None, max_length=4000)


def _validate_base_url(value: str) -> str:
    """校验上游地址：仅允许 http/https，禁止内嵌凭据与云元数据端点。"""
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ApiError("AI_PARAM_INVALID", "base_url must use http or https", 400)
    if not parsed.hostname:
        raise ApiError("AI_PARAM_INVALID", "base_url must include a host", 400)
    if parsed.hostname.lower() in _BLOCKED_HOSTS:
        raise ApiError("AI_PARAM_INVALID", "base_url must not target cloud metadata endpoints", 400)
    if parsed.username or parsed.password:
        raise ApiError("AI_PARAM_INVALID", "base_url must not embed credentials", 400)
    return url.rstrip("/")


@router.post("", summary="首次开通向导：一次配置模型与助手")
async def run_onboarding(
    payload: OnboardingRequest,
    request: Request,
    user: ModelManageUser,
    db: DbSession,
):
    """幂等编排：Secret → Provider → Model → Endpoint → 已发布 Agent → Conversation。"""
    base_url = _validate_base_url(payload.base_url)
    if payload.interface_type not in _INTERFACE_TYPES:
        raise ApiError("AI_PARAM_INVALID", "unsupported interface_type", 400)
    api_key = payload.api_key.strip()
    if not api_key:
        raise ApiError("AI_PARAM_INVALID", "api_key must not be empty", 400)

    # 幂等：同名且已发布的「我的助手」已存在时直接返回，避免重复创建一整套资源
    existing = db.scalar(
        select(Agent).where(
            Agent.tenant_id == user.tenant_id,
            Agent.owner_user_id == user.id,
            Agent.name == payload.assistant_name.strip(),
            Agent.status == "ACTIVE",
            Agent.current_published_version_id.is_not(None),
        )
    )
    if existing is not None:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.tenant_id == user.tenant_id,
                Conversation.owner_user_id == user.id,
                Conversation.agent_id == existing.id,
                Conversation.status == "ACTIVE",
            )
        )
        if conversation is None:
            conversation = Conversation(
                tenant_id=user.tenant_id,
                owner_user_id=user.id,
                agent_id=existing.id,
                title="新对话",
                status="ACTIVE",
            )
            db.add(conversation)
            db.commit()
            db.refresh(conversation)
        return success_response(
            request,
            {
                "already_exists": True,
                "provider_id": None,
                "model_id": None,
                "endpoint_id": None,
                "agent_id": str(existing.id),
                "conversation_id": str(conversation.id),
            },
        )

    secret_ref = f"secret://{SecretStore().encrypt(api_key)}"
    provider = ModelProvider(
        tenant_id=user.tenant_id,
        created_by=user.id,
        name=f"{payload.vendor.strip()}（开通向导）",
        provider_type=payload.interface_type,
        status="ENABLED",
        base_url=base_url,
        secret_ref=secret_ref,
        description="由首次开通向导创建",
        config={},
    )
    db.add(provider)
    db.flush()

    model = ModelResource(
        tenant_id=user.tenant_id,
        provider_id=provider.id,
        name=payload.model_id.strip(),
        model_identifier=payload.model_id.strip(),
        status="ENABLED",
        supports_streaming=True,
        supports_function_calling=True,
        context_window=None,
        max_output_tokens=None,
        description="由首次开通向导创建",
        config={"interface_type": payload.interface_type},
    )
    db.add(model)
    db.flush()

    endpoint = ModelEndpoint(
        tenant_id=user.tenant_id,
        provider_id=provider.id,
        model_id=model.id,
        name="默认接入点",
        endpoint_url=base_url,
        status="ENABLED",
        priority=0,
        description="由首次开通向导创建",
        config={},
    )
    db.add(endpoint)
    db.flush()

    agent = create_agent(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        name=payload.assistant_name.strip(),
        description="开通向导创建的助手，可随时修改配置和提示词。",
    )
    version = create_version(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        agent_id=agent.id,
        snapshot={
            "model": payload.model_id.strip(),
            "system_prompt": (payload.system_prompt or "").strip() or _DEFAULT_SYSTEM_PROMPT,
        },
    )
    publish_version(db, tenant_id=user.tenant_id, user_id=user.id, version_id=version.id)

    conversation = Conversation(
        tenant_id=user.tenant_id,
        owner_user_id=user.id,
        agent_id=agent.id,
        title="新对话",
        status="ACTIVE",
    )
    db.add(conversation)
    db.flush()

    record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="ONBOARDING_COMPLETED",
        resource_type="AGENT",
        resource_id=agent.id,
        summary="onboarding wizard completed",
        detail={"provider_id": str(provider.id), "model_id": str(model.id)},
        trace_id=request.state.trace_id,
    )
    db.commit()
    db.refresh(agent)
    db.refresh(conversation)

    return success_response(
        request,
        {
            "already_exists": False,
            "provider_id": str(provider.id),
            "model_id": str(model.id),
            "endpoint_id": str(endpoint.id),
            "agent_id": str(agent.id),
            "conversation_id": str(conversation.id),
        },
    )
