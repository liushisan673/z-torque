"""A2A 受控 API（v1.3 §6.3 / §6.5）。

- `POST /a2a/invoke`：入站 A2A 调用。**不**走 `get_current_user`：入站凭据由
  `identity.py` 用本域 JWT 自行认证并校验 `a2a.invoke`（v1 仅方案 A），
  否则会在 API 层多认证一次、且无法表达 A2A 自己的拒绝语义；
- `GET  /a2a/agents`：本地已发布 Agent 的只读发现投影（需登录 + `a2a.invoke`），
  不泄露草稿/提示词。

两条纪律：能力先于权限（未装配/未开启 → 503）；本模块不 import 插件包。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response, trace_id_from
from app.db.session import get_db
from app.models import User
from app.security.permissions import list_user_permissions

SERVICE_A2A_GATEWAY = "a2a_gateway"

router = APIRouter(prefix="/a2a", tags=["a2a"])


def _gateway(request: Request):
    gateway = getattr(request.app.state, SERVICE_A2A_GATEWAY, None)
    if gateway is None:
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "a2a_gateway plugin is not assembled", 503)
    if not getattr(gateway, "enabled", False):
        raise ApiError("AI_A2A_DISABLED", "A2A gateway is disabled (A2A_ENABLED=false)", 503)
    return gateway


@router.post("/invoke", summary="A2A 入站调用")
async def a2a_invoke(request: Request):
    gateway = _gateway(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise ApiError("AI_A2A_DENIED", "A2A payload must be valid JSON", 403) from exc
    result = await gateway.invoke(headers=request.headers, payload=payload, trace_id=trace_id_from(request))
    return success_response(request, result)


@router.get("/agents", summary="A2A Agent 发现（只读）")
async def a2a_agents(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    gateway = _gateway(request)
    if not getattr(gateway, "discovery", None) or not gateway.discovery:
        raise ApiError("AI_A2A_DISABLED", "A2A local discovery is disabled", 503)
    permissions = list_user_permissions(db, tenant_id=user.tenant_id, user_id=user.id)
    if "a2a.invoke" not in permissions:
        raise ApiError("AI_A2A_DENIED", "missing permission: a2a.invoke", 403)
    if not getattr(gateway, "is_local_discovery_enabled", lambda: True)():
        raise ApiError("AI_A2A_DISABLED", "A2A local discovery is disabled (A2A_LOCAL_DISCOVERY=false)", 503)
    items = gateway.discovery.list_agents(tenant_id=user.tenant_id, limit=limit)
    return success_response(request, {"items": items})
