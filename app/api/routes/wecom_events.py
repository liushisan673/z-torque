"""企微能力服务事件 Webhook（AI 中台接收方）。

路径由 ``WECOM_EVENT_PATH`` 决定（默认 ``/wecom/events``），不挂
``/api/z-torque`` 前缀 —— 能力服务按订阅配置直接推送到根路径。

纪律：
- 原始 body 参与验签，不能用 Pydantic/JSON body 参数改写；
- 验签失败 401，且不进入业务处理；
- 验签通过立即 ACK，再异步处理（能力服务要求 5 秒内返回）；
- 不创建订阅、不直连企业微信 API。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.services.wecom_events import WeComEvent, WeComEventService, verify_signature

logger = logging.getLogger("z_torque.wecom_events")


def get_wecom_event_service(request: Request) -> WeComEventService:
    """按 app.state 取服务；ASGITransport 不跑 lifespan 时惰性创建。"""
    service = getattr(request.app.state, "wecom_event_service", None)
    model_gateway = getattr(request.app.state, "model_gateway", None)
    skill_gateway = getattr(request.app.state, "skill_gateway", None)
    registry = getattr(request.app.state, "port_registry", None)
    short_memory = registry.get("memory.short") if registry is not None else None
    if service is None:
        service = WeComEventService(
            get_settings(),
            model_gateway=model_gateway,
            skill_gateway=skill_gateway,
            short_memory=short_memory,
        )
        request.app.state.wecom_event_service = service
    return service


async def receive_wecom_event(request: Request) -> JSONResponse:
    service = get_wecom_event_service(request)
    secret = service.subscription_secret
    if not secret:
        logger.error("WECOM_ALERT webhook rejected: WECOM_SUB_SECRET is not configured")
        return JSONResponse(status_code=503, content={"ok": False, "error": "webhook not configured"})

    raw_body = await request.body()
    timestamp = request.headers.get("x-wecom-timestamp", "")
    signature = request.headers.get("x-wecom-signature", "")
    if not verify_signature(secret=secret, timestamp=timestamp, raw_body=raw_body, signature=signature):
        logger.warning(
            "wecom event rejected: invalid signature event_id=%s event_type=%s",
            request.headers.get("x-wecom-event-id"),
            request.headers.get("x-wecom-event"),
        )
        return JSONResponse(status_code=401, content={"ok": False, "error": "invalid signature"})

    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        logger.warning(
            "wecom event rejected: invalid json event_id=%s event_type=%s",
            request.headers.get("x-wecom-event-id"),
            request.headers.get("x-wecom-event"),
        )
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid json"})

    event = WeComEvent.from_payload(payload, request.headers)
    if not event.event_id:
        logger.warning("wecom event rejected: missing event_id event_type=%s", event.event_type)
        return JSONResponse(status_code=400, content={"ok": False, "error": "missing event_id"})

    if not service.accept(event):
        logger.info("wecom event duplicate event_id=%s event_type=%s", event.event_id, event.event_type)
        return JSONResponse(status_code=200, content={"ok": True, "duplicate": True})

    logger.info("wecom event accepted event_id=%s event_type=%s", event.event_id, event.event_type)
    service.enqueue(event)
    return JSONResponse(status_code=200, content={"ok": True})


def build_router(path: str) -> APIRouter:
    """按配置路径构造路由；路径在启动时定稿（改 env 需重启）。"""
    router = APIRouter(tags=["wecom-events"])
    router.add_api_route(
        path,
        receive_wecom_event,
        methods=["POST"],
        name="wecom_event_webhook",
        include_in_schema=False,
    )
    return router
