from typing import Annotated
from uuid import UUID

import time

import httpx
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response
from app.core.servertime import iso_utc
from app.db.session import get_db
from app.kernel.audit.service import record_audit
from app.models import ModelEndpoint, ModelProvider, ModelResource, User
from app.schemas.model import (
    ModelEndpointCreate,
    ModelEndpointPatch,
    ModelProviderCreate,
    ModelProviderPatch,
    ModelResourceCreate,
    ModelResourcePatch,
)
from app.security.permissions import require_permissions
from app.security.secret_store import SecretStore, SecretStoreError

router = APIRouter(prefix="/models", tags=["models"])

CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[Session, Depends(get_db)]
# 写操作与探测统一要求 model.manage 权限（admin 角色经种子数据天然具备）
ModelManageUser = Annotated[User, Depends(require_permissions(["model.manage"]))]

INTERFACE_TYPES = {"openai_compatible", "anthropic", "custom"}
PROBE_MODES = {"readiness", "models_list", "minimal_chat"}
UPSTREAM_TIMEOUT_SECONDS = 10.0


def _validate_interface_type(value: str | None) -> None:
    if value is not None and value not in INTERFACE_TYPES:
        raise ApiError("AI_PARAM_INVALID", "interface_type must be one of: " + ", ".join(sorted(INTERFACE_TYPES)), 400)


def _validate_secret_ref(value: str | None) -> None:
    if value and not value.startswith("secret://"):
        raise ApiError("AI_PARAM_INVALID", "secret_ref must use Secret URI", 400)


def _mask_secret_ref(value: str | None) -> str | None:
    """脱敏 Secret URI：仅保留引用形态与末 4 位，任何场景不返回完整引用/明文。"""
    if not value:
        return None
    tail = value[-4:] if len(value) > 8 else "****"
    return f"secret://••••{tail}"


def serialize_provider(item: ModelProvider) -> dict:
    return {
        "id": str(item.id),
        "name": item.name,
        "provider_type": item.provider_type,
        "status": item.status,
        "base_url": item.base_url,
        "secret_configured": bool(item.secret_ref),
        "secret_ref_masked": _mask_secret_ref(item.secret_ref),
        "description": item.description,
        "config": item.config,
        "created_at": iso_utc(item.created_at),
    }


def serialize_model(item: ModelResource, provider: ModelProvider | None = None, endpoints: list[ModelEndpoint] | None = None) -> dict:
    payload = {
        "id": str(item.id),
        "provider_id": str(item.provider_id),
        "name": item.name,
        "model_identifier": item.model_identifier,
        "interface_type": item.config.get("interface_type") if item.config else None,
        "status": item.status,
        "supports_streaming": item.supports_streaming,
        "supports_function_calling": item.supports_function_calling,
        "context_window": item.context_window,
        "max_output_tokens": item.max_output_tokens,
        "description": item.description,
        "config": item.config,
        "created_at": iso_utc(item.created_at),
    }
    if provider is not None:
        payload["provider"] = serialize_provider(provider)
    if endpoints is not None:
        payload["endpoints"] = [serialize_endpoint(x) for x in endpoints]
    return payload


def serialize_endpoint(item: ModelEndpoint) -> dict:
    return {
        "id": str(item.id),
        "provider_id": str(item.provider_id),
        "model_id": str(item.model_id),
        "name": item.name,
        "endpoint_url": item.endpoint_url,
        "status": item.status,
        "priority": item.priority,
        "description": item.description,
        "config": item.config,
        "created_at": iso_utc(item.created_at),
    }


def get_owned_provider(db: Session, user: User, provider_id: UUID) -> ModelProvider:
    item = db.scalar(select(ModelProvider).where(ModelProvider.id == provider_id, ModelProvider.tenant_id == user.tenant_id))
    if not item:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "model provider not found", 404)
    return item


def get_owned_model(db: Session, user: User, model_id: UUID) -> ModelResource:
    item = db.scalar(select(ModelResource).where(ModelResource.id == model_id, ModelResource.tenant_id == user.tenant_id))
    if not item:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "model resource not found", 404)
    return item


def get_owned_endpoint(db: Session, user: User, endpoint_id: UUID) -> ModelEndpoint:
    item = db.scalar(select(ModelEndpoint).where(ModelEndpoint.id == endpoint_id, ModelEndpoint.tenant_id == user.tenant_id))
    if not item:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "model endpoint not found", 404)
    return item


# --- Provider routes ---


@router.get("/providers", summary="获取模型供应商列表")
async def list_providers(request: Request, user: CurrentUser, db: DbSession):
    items = db.scalars(select(ModelProvider).where(ModelProvider.tenant_id == user.tenant_id).order_by(ModelProvider.created_at)).all()
    return success_response(request, [serialize_provider(x) for x in items])


@router.post("/providers", summary="创建模型供应商")
async def create_provider(payload: ModelProviderCreate, request: Request, user: ModelManageUser, db: DbSession):
    _validate_secret_ref(payload.secret_ref)
    item = ModelProvider(
        tenant_id=user.tenant_id,
        created_by=user.id,
        name=payload.name,
        provider_type=payload.provider_type,
        status="ENABLED",
        base_url=payload.base_url,
        secret_ref=payload.secret_ref,
        description=payload.description,
        config=payload.config,
    )
    db.add(item); db.flush()
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="MODEL_PROVIDER_CREATED", resource_type="MODEL_PROVIDER", resource_id=item.id, summary="model provider created", trace_id=request.state.trace_id)
    db.commit(); db.refresh(item)
    return success_response(request, serialize_provider(item))


@router.get("/providers/{provider_id}", summary="获取模型供应商详情")
async def get_provider(provider_id: UUID, request: Request, user: CurrentUser, db: DbSession):
    return success_response(request, serialize_provider(get_owned_provider(db, user, provider_id)))


@router.patch("/providers/{provider_id}", summary="更新模型供应商")
async def patch_provider(provider_id: UUID, payload: ModelProviderPatch, request: Request, user: ModelManageUser, db: DbSession):
    item = get_owned_provider(db, user, provider_id)
    values = payload.model_dump(exclude_unset=True)
    if "secret_ref" in values:
        _validate_secret_ref(values["secret_ref"])
    for key, value in values.items(): setattr(item, key, value)
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="MODEL_PROVIDER_UPDATED", resource_type="MODEL_PROVIDER", resource_id=item.id, summary="model provider updated", detail={"fields": list(values)}, trace_id=request.state.trace_id)
    db.commit(); db.refresh(item)
    return success_response(request, serialize_provider(item))


@router.post("/providers/{provider_id}/disable", summary="停用模型供应商")
async def disable_provider(provider_id: UUID, request: Request, user: ModelManageUser, db: DbSession):
    item = get_owned_provider(db, user, provider_id); item.status = "DISABLED"
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="MODEL_PROVIDER_DISABLED", resource_type="MODEL_PROVIDER", resource_id=item.id, summary="model provider disabled", trace_id=request.state.trace_id)
    db.commit()
    return success_response(request, serialize_provider(item))


@router.post("/providers/{provider_id}/enable", summary="启用模型供应商")
async def enable_provider(provider_id: UUID, request: Request, user: ModelManageUser, db: DbSession):
    item = get_owned_provider(db, user, provider_id); item.status = "ENABLED"
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="MODEL_PROVIDER_ENABLED", resource_type="MODEL_PROVIDER", resource_id=item.id, summary="model provider enabled", trace_id=request.state.trace_id)
    db.commit()
    return success_response(request, serialize_provider(item))


# --- Endpoint routes ---


@router.get("/endpoints", summary="获取接入点列表")
async def list_endpoints(request: Request, user: CurrentUser, db: DbSession, model_id: UUID | None = Query(default=None)):
    query = select(ModelEndpoint).where(ModelEndpoint.tenant_id == user.tenant_id)
    if model_id is not None:
        query = query.where(ModelEndpoint.model_id == model_id)
    items = db.scalars(query.order_by(ModelEndpoint.priority, ModelEndpoint.created_at)).all()
    return success_response(request, [serialize_endpoint(x) for x in items])


@router.post("/endpoints", summary="创建接入点")
async def create_endpoint(payload: ModelEndpointCreate, request: Request, user: ModelManageUser, db: DbSession):
    provider = get_owned_provider(db, user, UUID(payload.provider_id))
    model = get_owned_model(db, user, UUID(payload.model_id))
    if model.provider_id != provider.id:
        raise ApiError("AI_PARAM_INVALID", "model does not belong to provider", 400)
    item = ModelEndpoint(
        tenant_id=user.tenant_id,
        provider_id=provider.id,
        model_id=model.id,
        name=payload.name,
        endpoint_url=payload.endpoint_url,
        status="ENABLED",
        priority=payload.priority,
        description=payload.description,
        config=payload.config,
    )
    db.add(item); db.flush()
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="MODEL_ENDPOINT_CREATED", resource_type="MODEL_ENDPOINT", resource_id=item.id, summary="model endpoint created", detail={"model_id": str(model.id)}, trace_id=request.state.trace_id)
    db.commit(); db.refresh(item)
    return success_response(request, serialize_endpoint(item))


@router.get("/endpoints/{endpoint_id}", summary="获取接入点详情")
async def get_endpoint(endpoint_id: UUID, request: Request, user: CurrentUser, db: DbSession):
    return success_response(request, serialize_endpoint(get_owned_endpoint(db, user, endpoint_id)))


@router.patch("/endpoints/{endpoint_id}", summary="更新接入点")
async def patch_endpoint(endpoint_id: UUID, payload: ModelEndpointPatch, request: Request, user: ModelManageUser, db: DbSession):
    item = get_owned_endpoint(db, user, endpoint_id)
    values = payload.model_dump(exclude_unset=True)
    if "provider_id" in values and values["provider_id"] is not None:
        provider = get_owned_provider(db, user, UUID(values["provider_id"]))
        values["provider_id"] = provider.id
    if "model_id" in values and values["model_id"] is not None:
        model = get_owned_model(db, user, UUID(values["model_id"]))
        values["model_id"] = model.id
    for key, value in values.items(): setattr(item, key, value)
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="MODEL_ENDPOINT_UPDATED", resource_type="MODEL_ENDPOINT", resource_id=item.id, summary="model endpoint updated", detail={"fields": list(values)}, trace_id=request.state.trace_id)
    db.commit(); db.refresh(item)
    return success_response(request, serialize_endpoint(item))


@router.post("/endpoints/{endpoint_id}/disable", summary="停用接入点")
async def disable_endpoint(endpoint_id: UUID, request: Request, user: ModelManageUser, db: DbSession):
    item = get_owned_endpoint(db, user, endpoint_id); item.status = "DISABLED"
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="MODEL_ENDPOINT_DISABLED", resource_type="MODEL_ENDPOINT", resource_id=item.id, summary="model endpoint disabled", trace_id=request.state.trace_id)
    db.commit()
    return success_response(request, serialize_endpoint(item))


@router.post("/endpoints/{endpoint_id}/enable", summary="启用接入点")
async def enable_endpoint(endpoint_id: UUID, request: Request, user: ModelManageUser, db: DbSession):
    item = get_owned_endpoint(db, user, endpoint_id); item.status = "ENABLED"
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="MODEL_ENDPOINT_ENABLED", resource_type="MODEL_ENDPOINT", resource_id=item.id, summary="model endpoint enabled", trace_id=request.state.trace_id)
    db.commit()
    return success_response(request, serialize_endpoint(item))


# --- Model resource routes ---


@router.get("", summary="获取模型列表")
async def list_models(request: Request, user: CurrentUser, db: DbSession, provider_id: UUID | None = Query(default=None)):
    query = select(ModelResource).where(ModelResource.tenant_id == user.tenant_id)
    if provider_id is not None:
        query = query.where(ModelResource.provider_id == provider_id)
    items = db.scalars(query.order_by(ModelResource.created_at)).all()
    # 列表一并返回各模型的 Endpoint（一次查询按 model_id 分组），
    # 否则前端列表标签因缺数据会恒显示「未连接」
    eps = db.scalars(
        select(ModelEndpoint)
        .where(ModelEndpoint.tenant_id == user.tenant_id)
        .order_by(ModelEndpoint.priority, ModelEndpoint.created_at)
    ).all()
    grouped: dict[str, list[ModelEndpoint]] = {}
    for e in eps:
        grouped.setdefault(str(e.model_id), []).append(e)
    return success_response(request, [serialize_model(x, endpoints=grouped.get(str(x.id), [])) for x in items])


@router.post("", summary="创建模型")
async def create_model(payload: ModelResourceCreate, request: Request, user: ModelManageUser, db: DbSession):
    _validate_interface_type(payload.interface_type)
    provider = get_owned_provider(db, user, UUID(payload.provider_id))
    if provider.status != "ENABLED":
        raise ApiError("AI_MODEL_NOT_AVAILABLE", "model provider is disabled", 503)
    # interface_type 无独立数据库列，统一存入 config JSON（读取时由 serialize_model 提为顶层字段）
    config = dict(payload.config)
    if payload.interface_type is not None:
        config["interface_type"] = payload.interface_type
    item = ModelResource(
        tenant_id=user.tenant_id,
        provider_id=provider.id,
        name=payload.name,
        model_identifier=payload.model_identifier,
        status="ENABLED",
        supports_streaming=payload.supports_streaming,
        supports_function_calling=payload.supports_function_calling,
        context_window=payload.context_window,
        max_output_tokens=payload.max_output_tokens,
        description=payload.description,
        config=config,
    )
    db.add(item); db.flush()
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="MODEL_RESOURCE_CREATED", resource_type="MODEL_RESOURCE", resource_id=item.id, summary="model resource created", detail={"provider_id": str(provider.id)}, trace_id=request.state.trace_id)
    db.commit(); db.refresh(item)
    return success_response(request, serialize_model(item))


@router.get("/{model_id}", summary="获取模型详情")
async def get_model(model_id: UUID, request: Request, user: CurrentUser, db: DbSession):
    item = get_owned_model(db, user, model_id)
    provider = db.scalar(select(ModelProvider).where(ModelProvider.id == item.provider_id))
    endpoints = db.scalars(select(ModelEndpoint).where(ModelEndpoint.model_id == item.id).order_by(ModelEndpoint.priority, ModelEndpoint.created_at)).all()
    return success_response(request, serialize_model(item, provider=provider, endpoints=endpoints))


@router.patch("/{model_id}", summary="更新模型")
async def patch_model(model_id: UUID, payload: ModelResourcePatch, request: Request, user: ModelManageUser, db: DbSession):
    item = get_owned_model(db, user, model_id)
    values = payload.model_dump(exclude_unset=True)
    # interface_type 非数据库列：显式传 null 表示清除，传值表示设置，统一合入 config JSON
    if "interface_type" in values:
        _validate_interface_type(values["interface_type"])
        interface_type = values.pop("interface_type")
        config = dict(item.config or {})
        if interface_type is None:
            config.pop("interface_type", None)
        else:
            config["interface_type"] = interface_type
        item.config = config
    if "provider_id" in values and values["provider_id"] is not None:
        provider = get_owned_provider(db, user, UUID(values["provider_id"]))
        if provider.status != "ENABLED":
            raise ApiError("AI_MODEL_NOT_AVAILABLE", "model provider is disabled", 503)
        values["provider_id"] = provider.id
    for key, value in values.items(): setattr(item, key, value)
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="MODEL_RESOURCE_UPDATED", resource_type="MODEL_RESOURCE", resource_id=item.id, summary="model resource updated", detail={"fields": list(values)}, trace_id=request.state.trace_id)
    db.commit(); db.refresh(item)
    return success_response(request, serialize_model(item))


@router.post("/{model_id}/disable", summary="停用模型")
async def disable_model(model_id: UUID, request: Request, user: ModelManageUser, db: DbSession):
    item = get_owned_model(db, user, model_id); item.status = "DISABLED"
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="MODEL_RESOURCE_DISABLED", resource_type="MODEL_RESOURCE", resource_id=item.id, summary="model resource disabled", trace_id=request.state.trace_id)
    db.commit()
    return success_response(request, serialize_model(item))


@router.post("/{model_id}/enable", summary="启用模型")
async def enable_model(model_id: UUID, request: Request, user: ModelManageUser, db: DbSession):
    item = get_owned_model(db, user, model_id); item.status = "ENABLED"
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="MODEL_RESOURCE_ENABLED", resource_type="MODEL_RESOURCE", resource_id=item.id, summary="model resource enabled", trace_id=request.state.trace_id)
    db.commit()
    return success_response(request, serialize_model(item))


@router.post("/{model_id}/test", summary="测试模型连接")
async def test_model(
    model_id: UUID,
    request: Request,
    user: ModelManageUser,
    db: DbSession,
    endpoint_id: UUID | None = Query(default=None),
    probe_mode: str = Query(default="readiness"),
):
    """模型连接测试。

    - readiness：网关配置就绪检查（不发起上游请求）
    - models_list：真实 GET 上游 /models
    - minimal_chat：真实 POST 上游 /chat/completions 最小请求
    探测失败统一返回 status=failed 与脱敏的 error_summary，不抛 500、不泄露异常栈与内部路径。
    """
    if probe_mode not in PROBE_MODES:
        raise ApiError("AI_PARAM_INVALID", "probe_mode must be one of: " + ", ".join(sorted(PROBE_MODES)), 400)
    item = get_owned_model(db, user, model_id)
    if item.status != "ENABLED":
        raise ApiError("AI_MODEL_NOT_AVAILABLE", "model is disabled", 503)
    provider = db.scalar(select(ModelProvider).where(ModelProvider.id == item.provider_id))
    if provider is None or provider.status != "ENABLED":
        raise ApiError("AI_MODEL_NOT_AVAILABLE", "model provider is unavailable", 503)
    if endpoint_id is not None:
        endpoint = get_owned_endpoint(db, user, endpoint_id)
        if endpoint.model_id != item.id:
            raise ApiError("AI_PARAM_INVALID", "endpoint does not belong to model", 400)
    else:
        endpoints = db.scalars(select(ModelEndpoint).where(ModelEndpoint.model_id == item.id, ModelEndpoint.status == "ENABLED").order_by(ModelEndpoint.priority)).all()
        endpoint = endpoints[0] if endpoints else None

    started = time.monotonic()
    checks = [
        {"name": "provider_enabled", "ok": True, "detail": provider.name},
        {"name": "model_enabled", "ok": True, "detail": item.model_identifier},
        {"name": "secret_configured", "ok": bool(provider.secret_ref), "detail": "已配置" if provider.secret_ref else "未配置"},
        {"name": "endpoint_available", "ok": endpoint is not None, "detail": endpoint.endpoint_url if endpoint else "无可用 Endpoint"},
    ]

    def result(status: str, *, http_status: int | None = None, summary: str | None = None, error_summary: str | None = None):
        return success_response(request, {
            "model_id": str(item.id),
            "status": status,
            "provider": {"id": str(provider.id), "name": provider.name, "status": provider.status},
            "endpoint": ({"id": str(endpoint.id), "name": endpoint.name, "endpoint_url": endpoint.endpoint_url, "status": endpoint.status} if endpoint else None),
            "checks": checks,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "trace_id": getattr(request.state, "trace_id", None),
            "probe_mode": probe_mode,
            "http_status": http_status,
            "summary": summary,
            "error_summary": error_summary,
            "supports_streaming": item.supports_streaming,
            "supports_function_calling": item.supports_function_calling,
            "secret_configured": bool(provider.secret_ref),
        })

    if probe_mode == "readiness":
        ok = endpoint is not None
        return result(
            "ready" if ok else "unavailable",
            summary="网关配置就绪检查通过" if ok else None,
            error_summary=None if ok else "无可用 Endpoint，就绪检查未通过",
        )

    # --- 真实上游探测（models_list / minimal_chat）---
    if endpoint is None:
        return result("unavailable", error_summary="无可用 Endpoint，无法发起真实探测")
    # ollama 类型上游不使用 Bearer 认证，其余必须持有可用密钥
    if "ollama" not in (provider.provider_type or "").lower():
        if not provider.secret_ref:
            return result("unavailable", error_summary="Provider 未配置 API 密钥，无法发起真实探测")
        try:
            api_key = SecretStore().decrypt(provider.secret_ref.removeprefix("secret://"))
        except SecretStoreError:
            return result("unavailable", error_summary="密钥解密失败，无法发起真实探测")
    else:
        api_key = None

    base_url = (endpoint.endpoint_url or "").rstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS) as client:
            if probe_mode == "models_list":
                resp = await client.get(f"{base_url}/models", headers=headers)
            else:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": item.model_identifier,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                        "stream": False,
                    },
                )
            resp.raise_for_status()
    except httpx.TimeoutException:
        return result("failed", error_summary="上游请求超时")
    except httpx.HTTPStatusError as exc:
        return result("failed", http_status=exc.response.status_code, error_summary=f"上游返回错误状态 {exc.response.status_code}")
    except httpx.HTTPError:
        return result("failed", error_summary="无法连接上游服务")
    except Exception:
        return result("failed", error_summary="探测执行失败")

    summary = "上游模型列表可访问" if probe_mode == "models_list" else "最小 Chat 请求成功"
    return result("ready", http_status=resp.status_code, summary=summary)
