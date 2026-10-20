"""MCP 连接器插件：Streamable HTTP 与 Stdio 传输实现（connector Port）。

自 app/kernel/skill_gateway.py（StreamableHttpMcpConnector）与
app/kernel/mcp_connectors.py（Stdio 三件套）整体迁移，逻辑保持不变；
内核只依赖 app/kernel/ports/connector.py 中的契约。
"""

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.kernel.ports.connector import McpProtocolError, McpTransportError
from app.security.secret_store import SecretStore

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MCP_PROTOCOL_VERSION = "2025-06-18"


class StreamableHttpMcpConnector:
    protocol_version = MCP_PROTOCOL_VERSION

    def __init__(self, endpoint: str, *, allowed_hosts: list[str], timeout_seconds: float = 30, max_response_bytes: int = 1_000_000, transport: httpx.AsyncBaseTransport | None = None, auth_token: str | None = None, extra_headers: dict[str, str] | None = None) -> None:
        self.endpoint = endpoint
        self.allowed_hosts = set(allowed_hosts)
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.auth_token = auth_token
        # 身份透传头（P2-5：SecurityContext 贯穿外部调用，X-ZT-User-Id 等，仅执行期）
        self.extra_headers = dict(extra_headers) if extra_headers else {}
        self.session_id: str | None = None
        self._request_id = 0
        self.client = httpx.AsyncClient(follow_redirects=False, timeout=timeout_seconds, transport=transport)

    async def negotiate(self) -> None:
        result = await self._request("initialize", {"protocolVersion": self.protocol_version, "capabilities": {}, "clientInfo": {"name": "z-torque", "version": "0.1.0"}}, include_session=False)
        version = result.get("protocolVersion")
        if version != self.protocol_version:
            raise McpProtocolError(f"unsupported MCP protocol version: {version}")
        await self._notification("notifications/initialized")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._request("tools/call", {"name": name, "arguments": arguments})

    async def list_tools(self) -> Any:
        return await self._request("tools/list", {})

    async def close(self) -> None:
        if self.session_id:
            try:
                await self.client.delete(self.endpoint, headers={"Mcp-Session-Id": self.session_id})
            finally:
                self.session_id = None
        await self.client.aclose()

    async def _notification(self, method: str) -> None:
        await self._send({"jsonrpc": "2.0", "method": method}, expect_response=False)

    async def _request(self, method: str, params: dict[str, Any], include_session: bool = True) -> dict[str, Any]:
        self._request_id += 1
        response = await self._send({"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}, expect_response=True, include_session=include_session)
        if "error" in response:
            raise McpProtocolError(str(response["error"]))
        return response.get("result", {})

    async def _send(self, payload: dict[str, Any], *, expect_response: bool, include_session: bool = True) -> dict[str, Any]:
        parsed = urlparse(self.endpoint)
        # 回环地址允许 http（本地联调）；其余目标强制 https 且必须在 allowed_hosts 白名单内
        if parsed.hostname in _LOOPBACK_HOSTS:
            if parsed.scheme not in ("https", "http"):
                raise McpTransportError("MCP endpoint is not allowed")
        elif parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts:
            raise McpTransportError("MCP endpoint is not allowed")
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": self.protocol_version}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        if self.extra_headers:
            headers.update(self.extra_headers)
        if include_session and self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            response = await self.client.post(self.endpoint, headers=headers, json=payload)
            if response.status_code >= 400:
                raise McpTransportError(f"MCP HTTP status {response.status_code}")
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id:
                self.session_id = session_id
            if not expect_response:
                return {}
            if len(response.content) > self.max_response_bytes:
                raise McpTransportError("MCP response exceeds configured limit")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type == "application/json":
                return response.json()
            if content_type == "text/event-stream":
                return self._parse_sse(response.text)
            raise McpTransportError("unsupported MCP response content type")
        except asyncio.CancelledError:
            raise
        except (httpx.TimeoutException, httpx.HTTPError, json.JSONDecodeError) as exc:
            raise McpTransportError(str(exc)) from exc

    @staticmethod
    def _parse_sse(body: str) -> dict[str, Any]:
        for block in body.split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
            if data:
                return json.loads(data)
        raise McpTransportError("MCP SSE response did not contain JSON-RPC data")


def make_http_connector(endpoint: str, *, allowed_hosts: list[str], timeout_seconds: float, max_response_bytes: int, auth_token: str | None = None, extra_headers: dict[str, str] | None = None) -> StreamableHttpMcpConnector:
    """SkillGateway 经该工厂创建 HTTP 连接器（内核不感知实现类）。"""
    return StreamableHttpMcpConnector(endpoint, allowed_hosts=allowed_hosts, timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes, auth_token=auth_token, extra_headers=extra_headers)


@dataclass(frozen=True)
class StdioServerRegistration:
    name: str
    executable: str
    arguments: tuple[str, ...]
    cwd: str
    environment: dict[str, str]

    def validate(self) -> None:
        executable = Path(self.executable)
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("stdio executable must be an existing absolute path")
        if any(not isinstance(arg, str) or "\x00" in arg for arg in self.arguments):
            raise ValueError("stdio arguments contain an invalid value")
        workdir = Path(self.cwd)
        if not workdir.is_absolute() or not workdir.is_dir():
            raise ValueError("stdio working directory must be an existing absolute path")
        if any(not isinstance(key, str) or not key or "=" in key or "\x00" in key for key in self.environment):
            raise ValueError("stdio environment contains an invalid variable name")
        if any(not isinstance(value, str) or "\x00" in value for value in self.environment.values()):
            raise ValueError("stdio environment contains an invalid value")

    def resolved_environment(self, secret_store: SecretStore) -> dict[str, str]:
        self.validate()
        resolved: dict[str, str] = {}
        for key, value in self.environment.items():
            if value.startswith("secret://"):
                encrypted = value.removeprefix("secret://")
                resolved[key] = secret_store.decrypt(encrypted)
            else:
                resolved[key] = value
        return resolved


class StdioServerRegistry:
    def __init__(self) -> None:
        self._registrations: dict[str, StdioServerRegistration] = {}

    def register(self, registration: StdioServerRegistration) -> None:
        registration.validate()
        if registration.name in self._registrations:
            raise ValueError("stdio server registration already exists")
        self._registrations[registration.name] = registration

    def get(self, name: str) -> StdioServerRegistration:
        try:
            return self._registrations[name]
        except KeyError as exc:
            raise KeyError("stdio server is not registered") from exc

    def connector(self, name: str, *, secret_store: SecretStore | None = None, timeout_seconds: float = 30, max_output_bytes: int = 1_000_000) -> "StdioMcpConnector":
        registration = self.get(name)
        store = secret_store or SecretStore()
        return StdioMcpConnector(registration.executable, list(registration.arguments), allowed_environment=registration.resolved_environment(store), cwd=registration.cwd, timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes)


class StdioMcpConnector:
    def __init__(self, executable: str, arguments: list[str], *, allowed_environment: dict[str, str], cwd: str, timeout_seconds: float = 30, max_output_bytes: int = 1_000_000) -> None:
        self.executable = executable
        self.arguments = arguments
        self.allowed_environment = allowed_environment
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.process: asyncio.subprocess.Process | None = None
        self._request_id = 0

    async def negotiate(self) -> None:
        if not os.path.isabs(self.executable) or not os.path.isfile(self.executable):
            raise McpTransportError("MCP stdio executable is not registered")
        self.process = await asyncio.create_subprocess_exec(self.executable, *self.arguments, cwd=self.cwd, env=self.allowed_environment, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stderr_task = asyncio.create_task(self._drain_stderr())
        self._stderr_task = stderr_task
        try:
            result = await self._request("initialize", {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "z-torque", "version": "0.1.0"}})
            if result.get("protocolVersion") != MCP_PROTOCOL_VERSION:
                raise McpProtocolError("unsupported MCP protocol version")
            await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_response=False)
        except BaseException:
            await self.close()
            raise

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return await self._request("tools/call", {"name": name, "arguments": arguments})

    async def close(self) -> None:
        if self.process is None:
            return
        if self.process.stdin:
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=self.timeout_seconds)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        stderr_task = getattr(self, "_stderr_task", None)
        if stderr_task:
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
        self.process = None

    async def _request(self, method: str, params: dict) -> dict:
        self._request_id += 1
        return await self._send({"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}, expect_response=True, expected_id=self._request_id)

    async def _send(self, payload: dict, *, expect_response: bool, expected_id: int | None = None) -> dict:
        if not self.process or not self.process.stdin or not self.process.stdout:
            raise McpTransportError("MCP stdio process is unavailable")
        self.process.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        await self.process.stdin.drain()
        if not expect_response:
            return {}
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise McpTransportError("MCP stdio request timed out") from exc
        if len(line) > self.max_output_bytes:
            raise McpTransportError("MCP stdio response exceeds configured limit")
        if not line:
            raise McpTransportError("MCP stdio process exited")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise McpProtocolError("invalid MCP stdio JSON-RPC message") from exc
        if message.get("id") != expected_id:
            raise McpProtocolError("MCP stdio response id mismatch")
        if "error" in message:
            raise McpProtocolError(str(message["error"]))
        return message.get("result", {})

    async def _drain_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        while await self.process.stderr.readline():
            pass
