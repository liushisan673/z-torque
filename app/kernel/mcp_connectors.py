import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

from app.kernel.skill_gateway import McpProtocolError, McpTransportError
from app.security.secret_store import SecretStore


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
            result = await self._request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "z-torque", "version": "0.1.0"}})
            if result.get("protocolVersion") != "2025-06-18":
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
