"""内核 Port 契约目录（阶段00 §3.5）。

内核只依赖本包中的契约（Protocol/数据类/异常），不依赖任何具体实现；
具体实现由 app/builtin_plugins 经装配根（app/assembly.py）提供。
"""

from app.kernel.ports.connector import McpConnector, McpDiscoverableConnector, McpProtocolError, McpTransportError
from app.kernel.ports.queue_driver import Executor, InMemoryQueueDriver, QueueDriver
from app.kernel.ports.skill_executor import AsyncToolExecutor, ExecutionResult, ToolExecutor
from app.kernel.ports.specs import (
    GROUPS,
    PORT_SPECS,
    PORT_SPEC_BY_NAME,
    GROUP_CROSS_CUTTING,
    GROUP_ENTRY,
    GROUP_EXECUTION,
    GROUP_INFORMATION,
    PortSpec,
)

__all__ = [
    "McpConnector",
    "McpDiscoverableConnector",
    "McpProtocolError",
    "McpTransportError",
    "Executor",
    "InMemoryQueueDriver",
    "QueueDriver",
    "AsyncToolExecutor",
    "ExecutionResult",
    "ToolExecutor",
    "GROUPS",
    "PORT_SPECS",
    "PORT_SPEC_BY_NAME",
    "GROUP_CROSS_CUTTING",
    "GROUP_ENTRY",
    "GROUP_EXECUTION",
    "GROUP_INFORMATION",
    "PortSpec",
]
