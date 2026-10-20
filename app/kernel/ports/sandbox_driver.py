from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class SandboxDriver(ABC):
    """sandbox.driver Port 契约：受控脚本沙箱执行（契约 §3 执行族）。

    输入经 stdin 传 JSON 参数、stdout 收 JSON 结果；超时 / 输出超限 / 非零退出
    抛 ApiError（AI_SKILL_TIMEOUT / AI_SKILL_FAILED）。社区版内置进程内受限执行
    （独立解释器 -I + 最小环境变量），企业版可替换为容器 / sidecar 隔离。
    """

    @abstractmethod
    async def run(self, *, script_path: Path, arguments: dict[str, Any], cwd: Path, timeout_seconds: float, max_output_bytes: int) -> dict[str, Any]:
        """在受控环境中执行脚本并返回解析后的 JSON 结果。"""
