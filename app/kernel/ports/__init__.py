"""内核 Port 契约目录（阶段00 §3.5）。

内核只依赖本包中的契约（Protocol/数据类/异常），不依赖任何具体实现；
具体实现由 app/builtin_plugins 经装配根（app/assembly.py）提供。
"""

from app.kernel.ports.authz_provider import AuthzProvider
from app.kernel.ports.connector import McpConnector, McpDiscoverableConnector, McpProtocolError, McpTransportError
from app.kernel.ports.identity_provider import IdentityProvider
from app.kernel.ports.lock_provider import LockProvider
from app.kernel.ports.model_selector import ModelSelection, ModelSelectionRequest, ModelSelector
from app.kernel.ports.queue_driver import Executor, QueueDriver
from app.kernel.ports.secret_provider import SecretProvider
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
    "AuthzProvider",
    "IdentityProvider",
    "LockProvider",
    "McpConnector",
    "McpDiscoverableConnector",
    "McpProtocolError",
    "McpTransportError",
    "ModelSelection",
    "ModelSelectionRequest",
    "ModelSelector",
    "SecretProvider",
    "Executor",
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
