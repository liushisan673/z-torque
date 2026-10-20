from abc import ABC, abstractmethod
from uuid import UUID


class NotifyChannel(ABC):
    """notify.channel Port 契约：租户级通知外发（契约 §3 入口族）。

    社区版内置日志/控制台通知（app/builtin_plugins/notify_channel.py），
    企业版可替换为邮件 / Webhook / 站内信等渠道实现。
    """

    @abstractmethod
    def send(self, *, tenant_id: UUID, user_id: UUID | None, title: str, body: str, level: str = "info", meta: dict | None = None) -> None:
        """发送一条通知；level ∈ info/warning/error；实现失败应抛 ApiError，不得静默吞掉。"""
