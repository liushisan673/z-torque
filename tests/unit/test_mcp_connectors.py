import asyncio
import sys

import pytest

from app.builtin_plugins.mcp import StdioMcpConnector, StdioServerRegistration, StdioServerRegistry
from app.kernel.ports.connector import McpProtocolError, McpTransportError


def test_stdio_registry_requires_absolute_existing_executable(tmp_path):
    registry = StdioServerRegistry()
    with pytest.raises(ValueError):
        registry.register(StdioServerRegistration("bad", "python", (), str(tmp_path), {}))


def test_stdio_registry_rejects_duplicate_registration(tmp_path):
    executable = sys.executable
    registration = StdioServerRegistration("server", executable, (), str(tmp_path), {})
    registry = StdioServerRegistry()
    registry.register(registration)
    with pytest.raises(ValueError):
        registry.register(registration)


class FakeSecretStore:
    def decrypt(self, value: str) -> str:
        assert value == "ciphertext"
        return "resolved-secret"


def test_stdio_registration_resolves_secret_uri(tmp_path):
    registration = StdioServerRegistration("server", sys.executable, (), str(tmp_path), {"TOKEN": "secret://ciphertext", "MODE": "safe"})
    assert registration.resolved_environment(FakeSecretStore()) == {"TOKEN": "resolved-secret", "MODE": "safe"}


def test_stdio_registry_builds_connector_from_registered_server(tmp_path):
    registration = StdioServerRegistration("server", sys.executable, (), str(tmp_path), {})
    registry = StdioServerRegistry()
    registry.register(registration)
    connector = registry.connector("server", secret_store=FakeSecretStore())
    assert connector.executable == sys.executable
    assert connector.cwd == str(tmp_path)


def _fake_server(tmp_path, mode="normal"):
    script = tmp_path / f"mcp_{mode}.py"
    script.write_text(
        "import json, sys, time\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        f"    {'time.sleep(5)' if mode == 'slow' else ''}\n"
        "    if request.get('method') == 'notifications/initialized': continue\n"
        "    result = {'protocolVersion': '2025-06-18'} if request.get('method') == 'initialize' else {'content': [{'type': 'text', 'text': 'ok'}]}\n"
        f"    response_id = request.get('id') {'+ 1' if mode == 'wrong_id' else ''}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': response_id, 'result': result}), flush=True)\n",
        encoding="utf-8",
    )
    return script


@pytest.mark.asyncio
async def test_stdio_fake_server_initialize_and_tool_call(tmp_path):
    connector = StdioMcpConnector(sys.executable, [str(_fake_server(tmp_path))], allowed_environment={}, cwd=str(tmp_path), timeout_seconds=1)
    await connector.negotiate()
    assert (await connector.call_tool("echo", {"value": 1}))["content"][0]["text"] == "ok"
    await connector.close()
    assert connector.process is None


@pytest.mark.asyncio
async def test_stdio_fake_server_rejects_response_id_mismatch(tmp_path):
    connector = StdioMcpConnector(sys.executable, [str(_fake_server(tmp_path, "wrong_id"))], allowed_environment={}, cwd=str(tmp_path), timeout_seconds=1)
    with pytest.raises(McpProtocolError, match="response id mismatch"):
        await connector.negotiate()
    assert connector.process is None


@pytest.mark.asyncio
async def test_stdio_fake_server_timeout_cleans_process(tmp_path):
    connector = StdioMcpConnector(sys.executable, [str(_fake_server(tmp_path, "slow"))], allowed_environment={}, cwd=str(tmp_path), timeout_seconds=0.05)
    with pytest.raises(McpTransportError, match="timed out"):
        await connector.negotiate()
    assert connector.process is None


@pytest.mark.asyncio
async def test_stdio_fake_server_cancellation_cleans_process(tmp_path):
    connector = StdioMcpConnector(sys.executable, [str(_fake_server(tmp_path, "slow"))], allowed_environment={}, cwd=str(tmp_path), timeout_seconds=2)
    task = asyncio.create_task(connector.negotiate())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connector.process is None


