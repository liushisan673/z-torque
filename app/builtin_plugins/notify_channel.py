"""notify.channel Port 的内置实现：日志/控制台通知（社区版，契约 §3.5 对照表）。

以结构化日志承载通知（不落库、不外发）；企业版可经 PortRegistry 以
PLUGIN/EXPLICIT 优先级替换为邮件 / Webhook / 站内信渠道实现。
"""

import logging
from uuid import UUID

from app.builtin_plugins.manifest import PluginManifest
from app.kernel.ports.notify_channel import NotifyChannel

logger = logging.getLogger("z_torque.notify")

_LEVELS = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


class LoggingNotifyChannel(NotifyChannel):
    def send(self, *, tenant_id: UUID, user_id: UUID | None, title: str, body: str, level: str = "info", meta: dict | None = None) -> None:
        logger.log(
            _LEVELS.get(level, logging.INFO),
            "notify tenant=%s user=%s title=%s body=%s meta=%s",
            tenant_id,
            user_id,
            title,
            body,
            meta or {},
        )


class NotifyChannelPlugin:
    manifest = PluginManifest(
        name="notify_channel",
        description="日志/控制台通知（社区版最小实现，邮件/Webhook 可选）",
        provides=("notify.channel",),
    )

    def contribute(self, sdk) -> None:
        sdk.register_port("notify.channel", LoggingNotifyChannel, label="builtin_logging_notify")
