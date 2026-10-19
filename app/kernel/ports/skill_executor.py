"""skill.executor Port 契约（阶段00 §3.5 Execution 族）。

工具执行器（ToolExecutor）是 skill.executor Port（SkillGateway）的内部策略：
网关按 tool_type 选择执行器，本文件只锁定执行器必须满足的结构化接口
（Protocol，鸭子类型，不要求继承）。
"""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    data: Any = None
    error_code: str | None = None
    error_message: str | None = None
    requires_confirmation: bool = False


class ToolExecutor(Protocol):
    """同步执行器：由网关经 asyncio.to_thread 调度。"""

    tool_type: str

    def execute(self, config: dict[str, Any], arguments: dict[str, Any], timeout_seconds: float) -> ExecutionResult: ...


class AsyncToolExecutor(Protocol):
    """异步执行器：由网关直接 await 并施加超时。"""

    tool_type: str

    async def execute_async(self, config: dict[str, Any], arguments: dict[str, Any]) -> ExecutionResult: ...
