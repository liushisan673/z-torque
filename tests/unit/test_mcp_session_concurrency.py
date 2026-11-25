import asyncio

import pytest

from app.kernel.ports.connector import McpRemoteError
from app.kernel.skill_gateway import SkillGateway


class FakeStdioConnector:
    def __init__(self):
        self.negotiate_calls = 0
        self.call_calls = 0
        self.close_calls = 0
        self.fail_once = False

    async def negotiate(self):
        self.negotiate_calls += 1
        await asyncio.sleep(0.01)

    async def call_tool(self, name, arguments):
        self.call_calls += 1
        await asyncio.sleep(0.01)
        if self.fail_once:
            self.fail_once = False
            raise McpRemoteError("tool returned a JSON-RPC error")
        return {"name": name, "arguments": arguments}

    async def close(self):
        self.close_calls += 1


class FakeStdioRegistry:
    def __init__(self, connector):
        self.connector_instance = connector
        self.create_calls = 0

    def connector(self, server_name, *, timeout_seconds):
        self.create_calls += 1
        return self.connector_instance


@pytest.mark.asyncio
async def test_concurrent_mcp_calls_share_one_initialized_session():
    connector = FakeStdioConnector()
    registry = FakeStdioRegistry(connector)
    gateway = SkillGateway(stdio_registry=registry)
    config = {"transport": "stdio", "server_name": "local-tools", "tool_name": "echo"}

    first, second = await asyncio.gather(
        gateway._execute_mcp(config, {"value": 1}, 2),
        gateway._execute_mcp(config, {"value": 2}, 2),
    )

    assert first.status == "SUCCEEDED"
    assert second.status == "SUCCEEDED"
    assert registry.create_calls == 1
    assert connector.negotiate_calls == 1
    assert connector.call_calls == 2
    await gateway.aclose_mcp_sessions()


@pytest.mark.asyncio
async def test_mcp_remote_tool_error_does_not_evict_healthy_session():
    connector = FakeStdioConnector()
    connector.fail_once = True
    registry = FakeStdioRegistry(connector)
    gateway = SkillGateway(stdio_registry=registry)
    config = {"transport": "stdio", "server_name": "local-tools", "tool_name": "echo"}

    failed = await gateway._execute_mcp(config, {}, 2)
    succeeded = await gateway._execute_mcp(config, {"retry": True}, 2)

    assert failed.status == "FAILED"
    assert failed.error_code == "AI_MCP_TOOL_CALL_FAILED"
    assert succeeded.status == "SUCCEEDED"
    assert registry.create_calls == 1
    assert connector.negotiate_calls == 1
    assert connector.close_calls == 0
    await gateway.aclose_mcp_sessions()
    assert connector.close_calls == 1
