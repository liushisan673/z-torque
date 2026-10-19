"""connector Port 契约（阶段00 §3.5 Execution 族）。

外部系统连接器（MCP 等）的传输层契约：内核（SkillGateway）只依赖本文件中的
Protocol 与异常类型；具体传输实现（Streamable HTTP / Stdio）由内置插件提供，
见 app/builtin_plugins/mcp.py。
"""

from typing import Any, Protocol, runtime_checkable


class McpTransportError(Exception):
    """MCP 传输层失败（网络、超时、协议载体不可用）。"""


class McpProtocolError(Exception):
    """MCP 协议层失败（握手版本不匹配、JSON-RPC error、响应不合法）。"""


@runtime_checkable
class McpConnector(Protocol):
    """对 MCP Server 的一次连接会话；每次工具调用走 negotiate → call_tool → close。"""

    async def negotiate(self) -> None: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...

    async def close(self) -> None: ...


@runtime_checkable
class McpDiscoverableConnector(Protocol):
    """具备工具发现能力的连接器（tools/list），用于连接器健康检查与工具发现。"""

    async def negotiate(self) -> None: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...

    async def list_tools(self) -> Any: ...

    async def close(self) -> None: ...


McpConnectorFactory = Any  # (endpoint: str, *, allowed_hosts: list[str], timeout_seconds: float, max_response_bytes: int) -> McpConnector
