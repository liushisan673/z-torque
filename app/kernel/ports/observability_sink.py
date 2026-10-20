from abc import ABC, abstractmethod
from typing import Any


class ObservabilitySink(ABC):
    """observability.sink Port 契约：结构化日志与事件出口（契约 §3 横切族）。

    社区版内置结构化日志（logging，logger 名 z_torque.observability），
    企业版可替换为 OTel / 日志平台等实现。
    """

    @abstractmethod
    def emit(self, event: str, payload: dict[str, Any] | None = None) -> None:
        """发射一条结构化事件；实现应尽力而为，不得中断业务。"""
