#!/usr/bin/env python3
"""
Z-Torque 本地 MCP 测试服务（仅联调，非生产）

协议：JSON-RPC 2.0
传输：HTTP POST（默认 http://127.0.0.1:8765/mcp）
方法：
  - initialize
  - tools/list
  - tools/call
  - ping

启动：
  python server.py
  python server.py --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

# 与客户端 StreamableHttpMcpConnector.protocol_version 保持一致（不一致握手会失败）
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "z-torque-mcp-test", "version": "1.0.0"}

# ---------------------------------------------------------------------------
# Demo tools
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "echo",
        "description": "原样返回文本，用于验证 tools/call 通路。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要回显的文本"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "计算两个数字之和。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "加数 a"},
                "b": {"type": "number", "description": "加数 b"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "get_time",
        "description": "返回当前 UTC 时间（ISO8601）。",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


def call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    args = arguments or {}
    if name == "echo":
        text = args.get("text", "")
        return {
            "content": [{"type": "text", "text": str(text)}],
            "isError": False,
        }
    if name == "add":
        try:
            a = float(args["a"])
            b = float(args["b"])
        except (KeyError, TypeError, ValueError) as e:
            return {
                "content": [{"type": "text", "text": f"参数错误: {e}"}],
                "isError": True,
            }
        return {
            "content": [{"type": "text", "text": str(a + b)}],
            "isError": False,
        }
    if name == "get_time":
        now = datetime.now(timezone.utc).isoformat()
        return {
            "content": [{"type": "text", "text": now}],
            "isError": False,
        }
    return {
        "content": [{"type": "text", "text": f"未知工具: {name}"}],
        "isError": True,
    }


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------

def jsonrpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def jsonrpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def handle_rpc(msg: dict[str, Any]) -> dict[str, Any] | None:
    """处理单条 JSON-RPC；notification 返回 None。"""
    if msg.get("jsonrpc") != "2.0":
        return jsonrpc_error(msg.get("id"), -32600, "Invalid Request: jsonrpc must be 2.0")

    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    # notification（无 id）不回复
    is_notification = "id" not in msg

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }
        return None if is_notification else jsonrpc_result(req_id, result)

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return None if is_notification else jsonrpc_result(req_id, {})

    if method == "tools/list":
        result = {"tools": TOOLS}
        return None if is_notification else jsonrpc_result(req_id, result)

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not name:
            return jsonrpc_error(req_id, -32602, "Missing params.name")
        result = call_tool(str(name), arguments if isinstance(arguments, dict) else {})
        return None if is_notification else jsonrpc_result(req_id, result)

    if is_notification:
        return None
    return jsonrpc_error(req_id, -32601, f"Method not found: {method}")


def process_body(raw: bytes) -> tuple[int, bytes]:
    try:
        data = json.loads(raw.decode("utf-8") or "null")
    except json.JSONDecodeError as e:
        err = jsonrpc_error(None, -32700, f"Parse error: {e}")
        return 400, json.dumps(err, ensure_ascii=False).encode("utf-8")

    # 支持 batch
    if isinstance(data, list):
        out: list[Any] = []
        for item in data:
            if not isinstance(item, dict):
                out.append(jsonrpc_error(None, -32600, "Invalid Request"))
                continue
            resp = handle_rpc(item)
            if resp is not None:
                out.append(resp)
        body = json.dumps(out, ensure_ascii=False).encode("utf-8")
        return 200, body

    if not isinstance(data, dict):
        err = jsonrpc_error(None, -32600, "Invalid Request")
        return 400, json.dumps(err, ensure_ascii=False).encode("utf-8")

    resp = handle_rpc(data)
    if resp is None:
        # notification → 204
        return 204, b""
    return 200, json.dumps(resp, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class MCPHandler(BaseHTTPRequestHandler):
    server_version = "ZTorqueMCPTest/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[mcp-test] %s - %s\n" % (self.address_string(), fmt % args))

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/health", "/mcp"):
            body = json.dumps(
                {
                    "ok": True,
                    "service": SERVER_INFO["name"],
                    "version": SERVER_INFO["version"],
                    "protocol": PROTOCOL_VERSION,
                    "endpoint": "POST /mcp  (JSON-RPC 2.0)",
                    "tools": [t["name"] for t in TOOLS],
                },
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path not in ("/mcp", "/"):
            self.send_response(404)
            self._cors()
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        status, body = process_body(raw)

        self.send_response(status)
        self._cors()
        if status == 204:
            self.end_headers()
            return
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Z-Torque local MCP test server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), MCPHandler)
    print(
        f"MCP test server listening on http://{args.host}:{args.port}/mcp\n"
        f"  GET  /health  → 服务信息\n"
        f"  POST /mcp     → JSON-RPC (initialize | tools/list | tools/call)\n"
        f"Tools: {', '.join(t['name'] for t in TOOLS)}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
        httpd.server_close()


if __name__ == "__main__":
    main()
