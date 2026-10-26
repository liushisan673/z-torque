"""插件可见的错误类型（再导出，保持异常身份一致）。

插件装配期失败必须抛 `AssemblyError`（由装配根终止启动并给出可诊断信息），
但标准插件不得 `from app.kernel.registry.port_registry import ...`，
故在此再导出同一对象：`app.plugin_sdk.errors.AssemblyError is kernel.AssemblyError`。
"""

from app.core.errors import ApiError
from app.kernel.registry.port_registry import AssemblyError

__all__ = ["ApiError", "AssemblyError"]
