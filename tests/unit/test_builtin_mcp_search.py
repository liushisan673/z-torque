import json
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.builtin_plugins.mcp import StreamableHttpMcpConnector
from app.config import get_settings
from app.db.session import SessionLocal
from app.kernel.skill_gateway import SkillGateway
from app.models import AgentVersion
from app.services.tool_mapping_builder import build_tool_mappings


class _McpTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests: list[tuple[dict, dict]] = []

    async def handle_async_request(self, request):
        if request.method == "DELETE":
            return httpx.Response(200, request=request)
        payload = json.loads(request.content)
        self.requests.append((payload, dict(request.headers)))
        if payload["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"}}
        elif payload["method"] == "tools/call":
            result = {"content": [{"type": "text", "text": "fake search result"}]}
        else:
            result = {"tools": []}
        body = {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}
        return httpx.Response(200, json=body, headers={"Mcp-Session-Id": "session-1"}, request=request)


def test_builtin_search_tools_in_tool_directory(monkeypatch):
    monkeypatch.setattr(
        "app.services.tool_mapping_builder.get_settings",
        lambda: SimpleNamespace(builtin_mcp_search_enabled=True, builtin_mcp_search_token="test-token"),
    )
    tenant_id = uuid4()
    version = AgentVersion(
        id=uuid4(), tenant_id=tenant_id, agent_id=uuid4(), version_number=1,
        status="PUBLISHED", config_snapshot={},
    )
    with SessionLocal() as db:
        mappings, tools = build_tool_mappings(db, version, tenant_id, user_id=None)

    names = {item["function"]["name"] for item in tools}
    assert {"web_search", "web_search_and_read", "output_document"} <= names
    assert mappings["web_search"].builtin_kind == "mcp_search"
    assert mappings["web_search"].original_name == "web_search"
    assert mappings["web_search_and_read"].builtin_kind == "mcp_search"


@pytest.mark.asyncio
async def test_execute_builtin_mcp_uses_injected_token_and_host():
    captured: dict = {}
    transport = _McpTransport()

    def factory(endpoint, **kwargs):
        captured.update(kwargs)
        return StreamableHttpMcpConnector(endpoint, transport=transport, **kwargs)

    gateway = SkillGateway(http_connector_factory=factory)
    result = await gateway.execute_builtin_mcp(
        endpoint="http://8.130.167.4:8765/mcp",
        tool_name="search",
        arguments={"q": "python 官方文档", "num": 2},
        token="test-token",
        timeout_seconds=5,
        allow_private_network=True,
    )

    assert result.status == "SUCCEEDED"
    assert result.data["content"][0]["text"] == "fake search result"
    assert captured["auth_token"] == "test-token"
    assert "8.130.167.4" in captured["allowed_hosts"]
    assert captured["allow_private_network"] is True
    assert captured["proxy"] == ""
    call = [item for item in transport.requests if item[0]["method"] == "tools/call"][-1]
    assert call[0]["params"]["name"] == "search"


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("RUN_LIVE_SEARCH_TEST") != "1", reason="需要显式开启真实网络搜索验证")
async def test_live_builtin_search():
    settings = get_settings()
    gateway = SkillGateway(http_connector_factory=StreamableHttpMcpConnector)
    token = settings.builtin_mcp_search_token.get_secret_value() if settings.builtin_mcp_search_token else None
    result = await gateway.execute_builtin_mcp(
        endpoint=settings.builtin_mcp_search_endpoint,
        tool_name="search",
        arguments={"q": "python 官方文档", "num": 2},
        token=token,
        timeout_seconds=settings.builtin_mcp_search_timeout_seconds,
        allow_private_network=settings.builtin_mcp_search_allow_private_network,
    )
    assert result.status == "SUCCEEDED", f"{result.error_code}: {result.error_message}"
    assert "python" in json.dumps(result.data, ensure_ascii=False).lower()
