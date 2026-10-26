"""Port 抽象基类的公开面（插件实现 Port 时需要的类型）。

插件只实现内核已声明的 Port，但不得 `from app.kernel.ports... import ...`：
Port 契约属于内核公开面，经本模块再导出（类型身份一致，isinstance 判定不变）。
"""

from app.kernel.ports.sandbox_driver import SandboxDriver

__all__ = ["SandboxDriver"]
