"""身份上下文契约（SecurityContext 再导出）。

SecurityContext 只承载身份事实（租户/用户/会话/追踪），不含权限裁决；
插件（如 a2a_gateway）需要构造/读取它，但不应依赖内核模块路径。
"""

from app.kernel.security_context import SecurityContext

__all__ = ["SecurityContext"]
