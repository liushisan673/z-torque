"""A2A v1.0 JSON-RPC endpoint plus the historical a2a.invoke compatibility API.

- `POST /a2a/invoke`：入站 A2A 调用。**不**走 `get_current_user`：入站凭据由
  `identity.py` 用本域 JWT 自行认证并校验 `a2a.invoke`（v1 仅方案 A），
  否则会在 API 层多认证一次、且无法表达 A2A 自己的拒绝语义；
- `GET  /a2a/agents`：本地已发布 Agent 的只读发现投影（需登录 + `a2a.invoke`），
  不泄露草稿/提示词。

两条纪律：能力先于权限（未装配/未开启 → 503）；本模块不 import 插件包。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.builtin_plugins.a2a_gateway.jsonrpc import (
    A2A_PROTOCOL_VERSION,
    jsonrpc_error,
    jsonrpc_result,
    parse_jsonrpc_request,
)
from app.core.errors import ApiError
from app.core.responses import success_response, trace_id_from
from app.db.session import get_db
from app.kernel.task.runtime import write_task_cancel_notice
from app.kernel.task.service import assert_agent_version_visible, create_task, transition_task
from app.kernel.task.states import TERMINAL_STATES, TaskStatus
from app.models import Agent, Task, User
from app.security.permissions import list_user_permissions

SERVICE_A2A_GATEWAY = "a2a_gateway"

router = APIRouter(prefix="/a2a", tags=["a2a"])

# 上游 peer 写入的链路头（网关出站用同名头，路由层不 import 插件包故按字面量约定）
_A2A_TRACE_HEADER = "x-a2a-trace"
_ALLOWED_TRACE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _inbound_trace_id(request: Request) -> str | None:
    """跨跳链路 id：优先沿用上游 X-A2A-Trace（格式受限，避免把任意串塞进审计列）。"""
    hop = (request.headers.get(_A2A_TRACE_HEADER) or "").strip()
    if hop and _ALLOWED_TRACE_RE.fullmatch(hop):
        return hop
    return trace_id_from(request)


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
    # 入站体积闸门：边读边计数，越界立即停止读取（不先把整个 body 读进内存再判长度）
    limit = int(getattr(gateway, "max_request_bytes", 1_000_000))
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise ApiError("AI_A2A_DENIED", f"A2A payload exceeds {limit} bytes", 403)
    raw = bytes(buffer)
    try:
        payload = json.loads(raw or b"{}")
    except ValueError as exc:
        raise ApiError("AI_A2A_DENIED", "A2A payload must be valid JSON", 403) from exc
    # 跨跳链路：沿用上游 X-A2A-Trace，否则每次跨实例调用都会断链、审计里看不到完整路径
    result = await gateway.invoke(headers=request.headers, payload=payload, trace_id=_inbound_trace_id(request))
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


# The well-known Agent Card is mounted at the protocol-defined server-root path.
well_known_router = APIRouter(tags=["a2a"])


def _a2a_task_state(task: Task) -> str:
    status = TaskStatus(task.status)
    if status in {TaskStatus.PENDING, TaskStatus.QUEUED}:
        return "submitted"
    if status in {TaskStatus.RUNNING, TaskStatus.WAITING_CHILD}:
        return "working"
    if status == TaskStatus.WAITING_HUMAN:
        return "input-required"
    if status == TaskStatus.SUCCESS:
        return "completed"
    if status == TaskStatus.CANCELLED:
        return "canceled"
    return "failed"


def _a2a_task(task: Task) -> dict:
    result = {
        "id": str(task.id),
        "contextId": str(task.conversation_id or task.root_task_id),
        "status": {
            "state": _a2a_task_state(task),
            "timestamp": task.updated_at.isoformat() if task.updated_at else None,
        },
    }
    if task.result is not None:
        result["artifacts"] = [{
            "artifactId": str(task.id),
            "name": "result",
            "parts": [{"kind": "data", "data": task.result}],
        }]
    if task.error_code:
        result["status"]["message"] = {
            "kind": "message",
            "messageId": f"{task.id}:status",
            "role": "ROLE_AGENT",
            "parts": [{"kind": "text", "text": task.error_message or task.error_code}],
        }
    return result


def _rpc_error(request_id, code: int, message: str) -> dict:
    return jsonrpc_error(request_id, code=code, message=message)


def _inbound_principal(request: Request, gateway):
    value = request.headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    return gateway.identity.authenticate(
        token.strip() if scheme.lower() == "bearer" and token.strip() else None,
        trace_id=_inbound_trace_id(request),
    )


def _standard_principal(request: Request, gateway):
    principal = _inbound_principal(request, gateway)
    # Task.owner_user_id is a mandatory FK. Do not invent an owner for a peer principal.
    if principal.ctx.user_id is None:
        raise ApiError("AI_A2A_DENIED", "federated Task operations require an explicitly mapped local service account", 403)
    return principal


@well_known_router.get("/.well-known/agent-card.json", include_in_schema=False)
async def standard_agent_card(request: Request):
    gateway = _gateway(request)
    # Local JWT and configured federated peer credentials may discover the explicitly
    # mapped workspace. Federated identity is accepted here only for read-only discovery.
    principal = _inbound_principal(request, gateway)
    if not gateway.is_local_discovery_enabled():
        raise ApiError("AI_A2A_DISABLED", "A2A local discovery is disabled", 503)
    agents = gateway.discovery.list_agents(tenant_id=principal.ctx.tenant_id, limit=200)
    skills = [{
        "id": str(item["agent_id"]),
        "name": str(item.get("name") or "Agent"),
        "description": str(item.get("description") or ""),
        "tags": ["z-torque-agent"],
        "examples": [],
    } for item in agents]
    return {
        "name": "Z-Torque",
        "description": "Published Z-Torque Agents exposed through A2A",
        "version": "1.0.0",
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "supportedInterfaces": [{
            "url": str(request.base_url).rstrip("/") + "/api/z-torque/a2a/jsonrpc",
            "protocolBinding": "JSONRPC",
            "protocolVersion": A2A_PROTOCOL_VERSION,
        }],
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": skills,
        "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "opaque"}},
        "security": [{"bearerAuth": []}],
    }


@router.post("/jsonrpc", summary="A2A v1.0 JSON-RPC endpoint")
async def standard_jsonrpc(request: Request, db: Session = Depends(get_db)):
    gateway = _gateway(request)
    limit = int(getattr(gateway, "max_request_bytes", 1_000_000))
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return _rpc_error(None, -32600, "request body exceeds configured limit")
    try:
        payload = json.loads(bytes(body) or b"{}")
        rpc = parse_jsonrpc_request(payload)
    except (ValueError, TypeError):
        return _rpc_error(None, -32700, "parse error")
    except ApiError as exc:
        request_id = payload.get("id") if isinstance(payload, dict) else None
        return _rpc_error(request_id, -32600, exc.message)

    try:
        principal = _standard_principal(request, gateway)
        ctx = principal.ctx
        if rpc.method == "SendMessage":
            message = rpc.params.get("message")
            if not isinstance(message, dict) or message.get("role") != "ROLE_USER":
                return _rpc_error(rpc.request_id, -32602, "params.message must be a user Message")
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            try:
                agent_id = UUID(str(metadata.get("z-torque/agentId")))
            except (TypeError, ValueError):
                return _rpc_error(rpc.request_id, -32602, "message.metadata['z-torque/agentId'] is required")
            parts = message.get("parts")
            if not isinstance(parts, list) or not parts:
                return _rpc_error(rpc.request_id, -32602, "message.parts must be a non-empty array")
            text_parts = [part.get("text", "") for part in parts if isinstance(part, dict) and part.get("kind", "text") == "text"]
            summary = "\n".join(str(value) for value in text_parts if value).strip()
            if not summary:
                return _rpc_error(rpc.request_id, -32602, "at least one text part is required")
            agent = db.scalar(select(Agent).where(
                Agent.id == agent_id,
                Agent.tenant_id == ctx.tenant_id,
                Agent.status == "ACTIVE",
                Agent.current_published_version_id.is_not(None),
            ))
            if agent is None:
                return _rpc_error(rpc.request_id, -32001, "target Agent is not available")
            version_id = agent.current_published_version_id
            assert_agent_version_visible(db, tenant_id=ctx.tenant_id, owner_user_id=ctx.user_id, agent_id=agent.id, agent_version_id=version_id)
            message_id = str(message.get("messageId") or rpc.request_id)
            task = create_task(
                db, ctx=ctx, agent_id=agent.id, agent_version_id=version_id,
                idempotency_key=("a2a:" + message_id)[:128],
                request_summary=summary[:4000], trace_id=_inbound_trace_id(request),
            )
            db.commit()
            db.refresh(task)
            return jsonrpc_result(rpc.request_id, {"task": _a2a_task(task)})

        if rpc.method in {"GetTask", "CancelTask"}:
            try:
                task_id = UUID(str(rpc.params.get("id")))
            except (TypeError, ValueError):
                return _rpc_error(rpc.request_id, -32602, "params.id must be a Task id")
            task = db.scalar(select(Task).where(
                Task.id == task_id, Task.tenant_id == ctx.tenant_id, Task.owner_user_id == ctx.user_id,
            ))
            if task is None:
                return _rpc_error(rpc.request_id, -32001, "Task not found")
            if rpc.method == "CancelTask":
                status = TaskStatus(task.status)
                if status not in TERMINAL_STATES:
                    if status in {TaskStatus.PENDING, TaskStatus.QUEUED}:
                        task.error_code = "AI_TASK_CANCELLED"
                        task.error_message = "cancelled by A2A caller"
                        transition_task(db, task, TaskStatus.CANCELLED, "TASK_CANCELLED", "A2A caller cancelled task", _inbound_trace_id(request), expected_epoch=task.attempt_epoch)
                        write_task_cancel_notice(db, task)
                    elif status == TaskStatus.RUNNING:
                        task.cancel_requested_at = datetime.now(timezone.utc)
                        transition_task(db, task, TaskStatus.CANCELING, "TASK_CANCEL_REQUESTED", "A2A caller requested cancellation", _inbound_trace_id(request))
                        runtime = getattr(request.app.state, "task_runtime", None)
                        if runtime is not None:
                            runtime.request_cancellation(task.id)
                    else:
                        return _rpc_error(rpc.request_id, -32002, "Task cannot be cancelled in its current state")
                    db.commit()
                    db.refresh(task)
            return jsonrpc_result(rpc.request_id, {"task": _a2a_task(task)})

        if rpc.method == "ListTasks":
            try:
                page_size = max(1, min(int(rpc.params.get("pageSize", 20)), 100))
                offset = max(0, int(rpc.params.get("pageToken", "0")))
            except (TypeError, ValueError):
                return _rpc_error(rpc.request_id, -32602, "invalid pageSize or pageToken")
            query = select(Task).where(
                Task.tenant_id == ctx.tenant_id, Task.owner_user_id == ctx.user_id,
            ).order_by(Task.created_at.desc())
            rows = db.scalars(query.offset(offset).limit(page_size + 1)).all()
            has_more = len(rows) > page_size
            rows = rows[:page_size]
            result = {"tasks": [_a2a_task(task) for task in rows], "pageSize": page_size}
            if has_more:
                result["nextPageToken"] = str(offset + page_size)
            return jsonrpc_result(rpc.request_id, result)

        return _rpc_error(rpc.request_id, -32601, f"method {rpc.method} is not implemented by this server")
    except ApiError as exc:
        return _rpc_error(rpc.request_id, -32003 if exc.status_code in (401, 403) else -32000, exc.message)
