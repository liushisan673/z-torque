"""observability.sink Port 的内置实现：结构化日志事件出口（契约 §3.5 对照表「内置结构化日志 + 事件」）。

以 JSON 结构化日志承载事件（logger 名 z_torque.observability）；企业版可经
PortRegistry 以 PLUGIN/EXPLICIT 优先级替换为 OTel / 日志平台等实现。
"""

import json
import logging

from app.builtin_plugins.manifest import PluginManifest
from app.kernel.ports.observability_sink import ObservabilitySink
from app.kernel.registry.port_registry import PortPriority

logger = logging.getLogger("z_torque.observability")


class LoggingObservabilitySink(ObservabilitySink):
    def emit(self, event: str, payload: dict | None = None) -> None:
        logger.info("%s %s", event, json.dumps(payload or {}, ensure_ascii=False, default=str))


class ObservabilitySinkPlugin:
    manifest = PluginManifest(
        name="observability_sink",
        description="结构化日志事件出口（z_torque.observability，可扩展）",
        provides=("observability.sink",),
    )

    def contribute(self, sdk) -> None:
        sdk.register_port("observability.sink", LoggingObservabilitySink, label="builtin_structured_logging")
