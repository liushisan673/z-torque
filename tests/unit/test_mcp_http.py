import json

import httpx
import pytest

from app.builtin_plugins.mcp import StreamableHttpMcpConnector
from app.kernel.ports.connector import McpProtocolError, McpTransportError


class McpTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, protocol="2025-06-18", content_type="application/json", status_code=200, sse=False):
        self.protocol = protocol
        self.content_type = content_type
        self.status_code = status_code
        self.sse = sse
        self.requests = []

    async def handle_async_request(self, request):
        if request.method == "DELETE":
            return httpx.Response(200, request=request)
        payload = json.loads(request.content)
        self.requests.append((payload, dict(request.headers)))
        if self.status_code != 200:
            return httpx.Response(self.status_code, request=request)
        if payload["method"] == "initialize":
            result = {"protocolVersion": self.protocol, "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"}}
        elif payload["method"] == "tools/call":
            result = {"content": [{"type": "text", "text": "ok"}]}
        else:
            result = {"tools": []}
        body = {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}
        if self.sse:
            return httpx.Response(200, headers={"content-type": "text/event-stream", "Mcp-Session-Id": "session-1"}, text=f"data: {json.dumps(body)}\n\n", request=request)
        return httpx.Response(200, headers={"content-type": self.content_type, "Mcp-Session-Id": "session-1"}, json=body, request=request)


@pytest.mark.asyncio
async def test_streamable_http_initialize_session_and_tool_call():
    transport = McpTransport()
    connector = StreamableHttpMcpConnector("https://mcp.example.test", allowed_hosts=["mcp.example.test"], transport=transport)
    await connector.negotiate()
    result = await connector.call_tool("echo", {"value": 1})
    assert result["content"][0]["text"] == "ok"
    assert transport.requests[1][1]["mcp-session-id"] == "session-1"
    await connector.close()


@pytest.mark.asyncio
async def test_streamable_http_sse_response():
    transport = McpTransport(sse=True)
    connector = StreamableHttpMcpConnector("https://mcp.example.test", allowed_hosts=["mcp.example.test"], transport=transport)
    await connector.negotiate()
    assert (await connector.list_tools()) == {"tools": []}
    await connector.close()


@pytest.mark.asyncio
async def test_streamable_http_rejects_protocol_mismatch():
    connector = StreamableHttpMcpConnector("https://mcp.example.test", allowed_hosts=["mcp.example.test"], transport=McpTransport(protocol="2026-07-28"))
    with pytest.raises(McpProtocolError, match="unsupported MCP protocol version"):
        await connector.negotiate()
    await connector.close()


@pytest.mark.asyncio
async def test_streamable_http_rejects_content_type_and_status():
    invalid_type = StreamableHttpMcpConnector("https://mcp.example.test", allowed_hosts=["mcp.example.test"], transport=McpTransport(content_type="text/plain"))
    with pytest.raises(McpTransportError, match="content type"):
        await invalid_type.negotiate()
    await invalid_type.close()
    failed = StreamableHttpMcpConnector("https://mcp.example.test", allowed_hosts=["mcp.example.test"], transport=McpTransport(status_code=405))
    with pytest.raises(McpTransportError, match="HTTP status"):
        await failed.negotiate()
    await failed.close()


@pytest.mark.asyncio
async def test_streamable_http_allows_loopback_http_without_whitelist():
    # 本地联调：回环地址允许 http，且不受 allowed_hosts 白名单约束
    transport = McpTransport()
    connector = StreamableHttpMcpConnector("http://127.0.0.1:8765/mcp", allowed_hosts=[], transport=transport)
    await connector.negotiate()
    assert (await connector.list_tools()) == {"tools": []}
    await connector.close()


@pytest.mark.asyncio
async def test_streamable_http_allows_loopback_localhost():
    transport = McpTransport()
    connector = StreamableHttpMcpConnector("http://localhost:8765/mcp", allowed_hosts=[], transport=transport)
    await connector.negotiate()
    await connector.close()


@pytest.mark.asyncio
async def test_streamable_http_sends_bearer_token_when_configured():
    # 配置了凭证时必须发送 Authorization: Bearer 头（如 TropaTT CRM 的 MCP 端点）
    transport = McpTransport()
    connector = StreamableHttpMcpConnector("https://mcp.example.test", allowed_hosts=["mcp.example.test"], transport=transport, auth_token="tok-123")
    await connector.negotiate()
    await connector.list_tools()
    assert transport.requests[0][1]["authorization"] == "Bearer tok-123"
    assert transport.requests[1][1]["authorization"] == "Bearer tok-123"
    await connector.close()


@pytest.mark.asyncio
async def test_streamable_http_omits_authorization_without_token():
    transport = McpTransport()
    connector = StreamableHttpMcpConnector("https://mcp.example.test", allowed_hosts=["mcp.example.test"], transport=transport)
    await connector.negotiate()
    assert "authorization" not in transport.requests[0][1]
    await connector.close()


@pytest.mark.asyncio
async def test_streamable_http_rejects_plain_http_remote():
    # 非回环目标强制 https：纯 http 远端拒绝
    connector = StreamableHttpMcpConnector("http://mcp.example.test/mcp", allowed_hosts=["mcp.example.test"], transport=McpTransport())
    with pytest.raises(McpTransportError, match="not allowed"):
        await connector.negotiate()
    await connector.close()


@pytest.mark.asyncio
async def test_streamable_http_rejects_host_not_in_whitelist():
    # https 远端必须在 allowed_hosts 白名单内
    connector = StreamableHttpMcpConnector("https://evil.example.test/mcp", allowed_hosts=["mcp.example.test"], transport=McpTransport())
    with pytest.raises(McpTransportError, match="not allowed"):
        await connector.negotiate()
    await connector.close()
