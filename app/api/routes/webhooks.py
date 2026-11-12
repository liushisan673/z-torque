"""通用入站 Webhook：管理面 CRUD + 入站接收端点。

管理面挂在 ``/api/z-torque/webhooks``（需登录）；入站端点是根路径
``POST /webhooks/{id}``，由外部系统按配置的签名方式直接调用，不走用户鉴权。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.responses import success_response
from app.core.servertime import iso_utc
from app.db.session import get_db
from app.kernel.audit.service import record_audit
from app.models import Agent, InboundWebhook, InboundWebhookEvent, User
from app.schemas.webhook import WebhookCreateRequest, WebhookPatchRequest
from app.security.permissions import require_permissions
from app.security.secret_store import SecretStore
from app.services.inbound_webhook import MAX_BODY_BYTES, InboundWebhookService

logger = logging.getLogger("z_torque.inbound_webhook")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
inbound_router = APIRouter(tags=["webhooks-inbound"], include_in_schema=False)

ReadUser = Annotated[User, Depends(require_permissions(["connector.read"]))]
ManageUser = Annotated[User, Depends(require_permissions(["connector.manage"]))]


def _agent_name_map(db: Session, agent_ids: set[UUID]) -> dict[UUID, str]:
    if not agent_ids:
        return {}
    rows = db.scalars(select(Agent).where(Agent.id.in_(agent_ids))).all()
    return {row.id: row.name for row in rows}


def _serialize(item: InboundWebhook, agent_name: str | None) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "name": item.name,
        "description": item.description,
        "enabled": bool(item.enabled),
        "target_agent_id": str(item.target_agent_id),
        "target_agent_name": agent_name,
        "auth_mode": item.auth_mode,
        "signature_header": item.signature_header,
        "timestamp_header": item.timestamp_header,
        "event_id_header": item.event_id_header,
        "token_header": item.token_header,
        "token_prefix": item.token_prefix,
        "signature_algorithm": item.signature_algorithm,
        "signature_encoding": item.signature_encoding,
        "signature_prefix": item.signature_prefix,
        "signature_template": item.signature_template,
        "timestamp_tolerance_seconds": item.timestamp_tolerance_seconds,
        "message_path": item.message_path,
        "conversation_key_path": item.conversation_key_path,
        "event_id_path": item.event_id_path,
        "media_path": item.media_path,
        "media_type_path": item.media_type_path,
        "media_source_path": item.media_source_path,
        "media_mime_path": item.media_mime_path,
        "media_file_name_path": item.media_file_name_path,
        "media_placeholder_texts": item.media_placeholder_texts or [],
        "reply_url": item.reply_url,
        "reply_token_configured": bool(item.reply_token_ciphertext),
        "reply_account_path": item.reply_account_path,
        "reply_peer_path": item.reply_peer_path,
        "inbound_path": f"/webhooks/{item.id}",
        "secret_configured": bool(item.secret_ciphertext),
        "last_received_at": iso_utc(item.last_received_at) if item.last_received_at else None,
        "created_at": iso_utc(item.created_at),
    }


def _get_webhook(db: Session, user: User, webhook_id: UUID) -> InboundWebhook:
    item = db.scalar(select(InboundWebhook).where(InboundWebhook.id == webhook_id, InboundWebhook.tenant_id == user.tenant_id))
    if item is None:
        raise ApiError("AI_WEBHOOK_NOT_FOUND", "webhook not found", 404)
    return item


def _require_agent(db: Session, user: User, agent_id: UUID) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None or agent.tenant_id != user.tenant_id:
        raise ApiError("AI_AGENT_NOT_FOUND", "agent not found", 404)
    if agent.status != "ACTIVE" or agent.current_published_version_id is None:
        raise ApiError("AI_WEBHOOK_AGENT_UNAVAILABLE", "agent must be active and published", 409)
    return agent


@router.get("", summary="获取 Webhook 列表")
async def list_webhooks(request: Request, user: ReadUser, db: Session = Depends(get_db)):
    rows = db.scalars(
        select(InboundWebhook)
        .where(InboundWebhook.tenant_id == user.tenant_id)
        .order_by(InboundWebhook.created_at.desc())
    ).all()
    names = _agent_name_map(db, {row.target_agent_id for row in rows})
    return success_response(request, [_serialize(row, names.get(row.target_agent_id)) for row in rows])


@router.post("", summary="创建 Webhook")
async def create_webhook(payload: WebhookCreateRequest, request: Request, user: ManageUser, db: Session = Depends(get_db)):
    _require_agent(db, user, payload.target_agent_id)
    # 密钥只回显一次：库里只存 Fernet 密文，列表/详情只给 secret_configured
    secret = secrets.token_urlsafe(32)
    item = InboundWebhook(
        tenant_id=user.tenant_id,
        owner_user_id=user.id,
        name=payload.name.strip(),
        description=payload.description,
        secret_ciphertext=SecretStore().encrypt(secret),
        auth_mode=payload.auth_mode,
        signature_header=payload.signature_header,
        timestamp_header=payload.timestamp_header,
        event_id_header=payload.event_id_header,
        token_header=payload.token_header,
        token_prefix=payload.token_prefix,
        signature_algorithm=payload.signature_algorithm,
        signature_encoding=payload.signature_encoding,
        signature_prefix=payload.signature_prefix,
        signature_template=payload.signature_template,
        timestamp_tolerance_seconds=payload.timestamp_tolerance_seconds,
        target_agent_id=payload.target_agent_id,
        message_path=payload.message_path,
        conversation_key_path=payload.conversation_key_path,
        event_id_path=payload.event_id_path,
        media_path=(payload.media_path or "").strip() or None,
        media_type_path=payload.media_type_path,
        media_source_path=payload.media_source_path,
        media_mime_path=payload.media_mime_path,
        media_file_name_path=payload.media_file_name_path,
        media_placeholder_texts=payload.media_placeholder_texts or None,
        reply_url=(payload.reply_url or "").strip() or None,
        reply_token_ciphertext=SecretStore().encrypt(payload.reply_token.strip())
        if payload.reply_token and payload.reply_token.strip()
        else None,
        reply_account_path=payload.reply_account_path,
        reply_peer_path=payload.reply_peer_path,
        enabled=payload.enabled,
    )
    db.add(item)
    db.flush()
    record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="WEBHOOK_CREATED",
        resource_type="WEBHOOK",
        resource_id=item.id,
        summary="inbound webhook created",
        detail={"agent_id": str(item.target_agent_id), "auth_mode": item.auth_mode},
    )
    db.commit()
    db.refresh(item)
    agent = db.get(Agent, item.target_agent_id)
    data = _serialize(item, agent.name if agent else None)
    data["secret"] = secret
    return success_response(request, data)


@router.get("/{webhook_id}", summary="获取 Webhook 详情")
async def get_webhook(webhook_id: UUID, request: Request, user: ReadUser, db: Session = Depends(get_db)):
    item = _get_webhook(db, user, webhook_id)
    agent = db.get(Agent, item.target_agent_id)
    return success_response(request, _serialize(item, agent.name if agent else None))


@router.patch("/{webhook_id}", summary="更新 Webhook 配置")
async def patch_webhook(webhook_id: UUID, payload: WebhookPatchRequest, request: Request, user: ManageUser, db: Session = Depends(get_db)):
    item = _get_webhook(db, user, webhook_id)
    values = payload.model_dump(exclude_unset=True)
    if "target_agent_id" in values and values["target_agent_id"] is not None:
        _require_agent(db, user, values["target_agent_id"])
    if "name" in values and values["name"] is not None:
        values["name"] = values["name"].strip()
    # 回写令牌只写不读：空串/None 表示清除，非空则重新加密
    if "reply_token" in values:
        token = values.pop("reply_token")
        token_text = (token or "").strip()
        item.reply_token_ciphertext = SecretStore().encrypt(token_text) if token_text else None
    if "reply_url" in values and not (values["reply_url"] or "").strip():
        values["reply_url"] = None
    if "media_path" in values and not (values["media_path"] or "").strip():
        values["media_path"] = None
    if "media_placeholder_texts" in values and not values["media_placeholder_texts"]:
        values["media_placeholder_texts"] = None
    for key, value in values.items():
        setattr(item, key, value)
    record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="WEBHOOK_UPDATED",
        resource_type="WEBHOOK",
        resource_id=item.id,
        summary="inbound webhook updated",
        detail={"fields": sorted(values.keys())},
    )
    db.commit()
    db.refresh(item)
    agent = db.get(Agent, item.target_agent_id)
    return success_response(request, _serialize(item, agent.name if agent else None))


@router.post("/{webhook_id}/rotate-secret", summary="轮换 Webhook 签名密钥")
async def rotate_secret(webhook_id: UUID, request: Request, user: ManageUser, db: Session = Depends(get_db)):
    item = _get_webhook(db, user, webhook_id)
    secret = secrets.token_urlsafe(32)
    item.secret_ciphertext = SecretStore().encrypt(secret)
    record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="WEBHOOK_SECRET_ROTATED",
        resource_type="WEBHOOK",
        resource_id=item.id,
        summary="inbound webhook secret rotated",
    )
    db.commit()
    agent = db.get(Agent, item.target_agent_id)
    data = _serialize(item, agent.name if agent else None)
    data["secret"] = secret
    return success_response(request, data)


@router.delete("/{webhook_id}", summary="删除 Webhook")
async def delete_webhook(webhook_id: UUID, request: Request, user: ManageUser, db: Session = Depends(get_db)):
    item = _get_webhook(db, user, webhook_id)
    db.query(InboundWebhookEvent).filter(InboundWebhookEvent.webhook_id == item.id).delete(synchronize_session=False)
    record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="WEBHOOK_DELETED",
        resource_type="WEBHOOK",
        resource_id=item.id,
        summary="inbound webhook deleted",
    )
    db.delete(item)
    db.commit()
    return success_response(request, {"id": str(webhook_id), "deleted": True})


# ---- 入站（外部系统直接调用，不走用户鉴权） -------------------------


def get_inbound_service(request: Request) -> InboundWebhookService:
    service = getattr(request.app.state, "inbound_webhook_service", None)
    if service is None:
        registry = getattr(request.app.state, "port_registry", None)
        short_memory = registry.get("memory.short") if registry is not None else None
        object_store = registry.get("object.store") if registry is not None else None
        service = InboundWebhookService(
            model_gateway=getattr(request.app.state, "model_gateway", None),
            skill_gateway=getattr(request.app.state, "skill_gateway", None),
            short_memory=short_memory,
            object_store=object_store,
        )
        request.app.state.inbound_webhook_service = service
    return service


async def _read_limited_body(request: Request, limit: int) -> bytes | None:
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > limit:
            return None
    return bytes(buffer)


@inbound_router.post("/webhooks/{webhook_id}")
async def receive_webhook(webhook_id: UUID, request: Request):
    service = get_inbound_service(request)
    config = await asyncio.to_thread(service.load, webhook_id)
    if config is None or not config.enabled:
        return JSONResponse(status_code=404, content={"ok": False, "error": "webhook not found"})

    raw_body = await _read_limited_body(request, MAX_BODY_BYTES)
    if raw_body is None:
        return JSONResponse(status_code=413, content={"ok": False, "error": "payload too large"})
    if not service.authenticate(config, raw_body, request.headers):
        logger.warning("inbound webhook rejected: invalid signature webhook_id=%s", webhook_id)
        return JSONResponse(status_code=401, content={"ok": False, "error": "invalid signature"})
    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid json"})

    parsed = service.parse(config, payload, request.headers, raw_body)
    if parsed is None:
        return JSONResponse(status_code=422, content={"ok": False, "error": "message field is empty"})

    event_row_id = await asyncio.to_thread(service.claim, config, parsed.event_id)
    if event_row_id is None:
        return JSONResponse(status_code=200, content={"ok": True, "duplicate": True, "event_id": parsed.event_id})

    service.enqueue(config, event_row_id, parsed.event_id, parsed.text, parsed.conversation_key, parsed=parsed)
    logger.info("inbound webhook accepted webhook_id=%s event_id=%s", webhook_id, parsed.event_id)
    return JSONResponse(status_code=202, content={"ok": True, "accepted": True, "event_id": parsed.event_id})
