"""A2A v1.0 JSON-RPC 2.0 wire helpers.

This module validates the JSON-RPC binding boundary only. It deliberately does not
translate the legacy skill-invocation envelope into an A2A Message or Task.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.core.errors import ApiError

JSONRPC_VERSION = "2.0"
A2A_PROTOCOL_VERSION = "1.0"
JSONRPC_BINDING = "JSONRPC"
A2A_JSONRPC_METHODS = frozenset(
    {
        "SendMessage",
        "SendStreamingMessage",
        "GetTask",
        "ListTasks",
        "CancelTask",
        "SubscribeToTask",
        "CreateTaskPushNotificationConfig",
        "GetTaskPushNotificationConfig",
        "ListTaskPushNotificationConfigs",
        "DeleteTaskPushNotificationConfig",
        "GetExtendedAgentCard",
    }
)


@dataclass(frozen=True)
class JsonRpcRequest:
    request_id: str | int
    method: str
    params: dict[str, Any]


def parse_jsonrpc_request(payload: Any) -> JsonRpcRequest:
    """Validate a JSON-RPC 2.0 request using A2A method names."""
    if not isinstance(payload, dict):
        raise ApiError("AI_A2A_JSONRPC_INVALID", "JSON-RPC request must be an object", 400)
    if payload.get("jsonrpc") != JSONRPC_VERSION:
        raise ApiError("AI_A2A_JSONRPC_INVALID", "jsonrpc must be '2.0'", 400)
    request_id = payload.get("id")
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        raise ApiError("AI_A2A_JSONRPC_INVALID", "A2A JSON-RPC request id must be a string or integer", 400)
    method = payload.get("method")
    if not isinstance(method, str) or method not in A2A_JSONRPC_METHODS:
        raise ApiError("AI_A2A_JSONRPC_METHOD_NOT_FOUND", "unknown A2A JSON-RPC method", 404)
    params = payload.get("params", {})
    if not isinstance(params, dict):
        raise ApiError("AI_A2A_JSONRPC_INVALID", "JSON-RPC params must be an object", 400)
    return JsonRpcRequest(request_id=request_id, method=method, params=params)


def jsonrpc_result(request_id: str | int, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def jsonrpc_error(
    request_id: str | int | None,
    *,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": int(code), "message": str(message)}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def validate_jsonrpc_response(payload: Any, *, expected_id: str | int) -> dict[str, Any]:
    """Reject mismatched ids and malformed response envelopes from remote peers."""
    if not isinstance(payload, dict) or payload.get("jsonrpc") != JSONRPC_VERSION:
        raise ApiError("AI_A2A_PEER_UNAVAILABLE", "peer returned an invalid JSON-RPC response", 503)
    if payload.get("id") != expected_id:
        raise ApiError("AI_A2A_PEER_UNAVAILABLE", "peer returned a mismatched JSON-RPC response id", 503)
    has_result = "result" in payload
    has_error = "error" in payload
    if has_result == has_error:
        raise ApiError("AI_A2A_PEER_UNAVAILABLE", "JSON-RPC response must contain exactly one of result or error", 503)
    if has_result and not isinstance(payload["result"], dict):
        raise ApiError("AI_A2A_PEER_UNAVAILABLE", "A2A JSON-RPC result must be an object", 503)
    if has_error:
        error = payload["error"]
        if not isinstance(error, dict) or isinstance(error.get("code"), bool) or not isinstance(error.get("code"), int) or not isinstance(error.get("message"), str):
            raise ApiError("AI_A2A_PEER_UNAVAILABLE", "peer returned an invalid JSON-RPC error object", 503)
    return payload


def validate_agent_card(payload: Any) -> dict[str, Any]:
    """Validate the minimum A2A v1.0 Agent Card fields needed for JSON-RPC routing."""
    if not isinstance(payload, dict):
        raise ApiError("AI_A2A_PEER_UNAVAILABLE", "peer Agent Card must be a JSON object", 503)
    if payload.get("protocolVersion") not in (None, A2A_PROTOCOL_VERSION):
        raise ApiError("AI_A2A_PEER_UNAVAILABLE", "peer Agent Card has an unsupported A2A protocol version", 503)
    interfaces = payload.get("supportedInterfaces")
    if not isinstance(interfaces, list) or not interfaces:
        raise ApiError("AI_A2A_PEER_UNAVAILABLE", "peer Agent Card has no supportedInterfaces", 503)
    candidates = []
    for item in interfaces:
        if not isinstance(item, dict):
            continue
        if item.get("protocolBinding") != JSONRPC_BINDING or item.get("protocolVersion") != A2A_PROTOCOL_VERSION:
            continue
        url = item.get("url")
        if not isinstance(url, str):
            continue
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
            continue
        candidates.append(item)
    if not candidates:
        raise ApiError("AI_A2A_PEER_UNAVAILABLE", "peer Agent Card does not advertise an A2A v1.0 JSONRPC interface", 503)
    if not isinstance(payload.get("name"), str) or not payload["name"].strip():
        raise ApiError("AI_A2A_PEER_UNAVAILABLE", "peer Agent Card is missing name", 503)
    return {**payload, "supportedInterfaces": candidates}


def validate_send_message_result(payload: dict[str, Any]) -> dict[str, Any]:
    """A2A SendMessageResponse must contain exactly one Task or Message."""
    has_task = "task" in payload
    has_message = "message" in payload
    if has_task == has_message:
        raise ApiError("AI_A2A_PEER_UNAVAILABLE", "A2A SendMessage result must contain exactly one task or message", 503)
    value = payload["task"] if has_task else payload["message"]
    if not isinstance(value, dict):
        raise ApiError("AI_A2A_PEER_UNAVAILABLE", "A2A SendMessage task/message must be an object", 503)
    if has_task and not isinstance(value.get("id"), str):
        raise ApiError("AI_A2A_PEER_UNAVAILABLE", "A2A Task result is missing its id", 503)
    return payload
