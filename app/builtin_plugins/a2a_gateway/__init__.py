"""a2a_gateway 插件（v1.3 M3）：A2A 协议标准化、发现、路由与会话。

导出符号仅限插件类；包内模块禁止在 import 期做 IO / 读配置 / 连库。
"""

from app.builtin_plugins.a2a_gateway.plugin import SERVICE_A2A_GATEWAY, A2AGatewayPlugin

__all__ = ["A2AGatewayPlugin", "SERVICE_A2A_GATEWAY"]
