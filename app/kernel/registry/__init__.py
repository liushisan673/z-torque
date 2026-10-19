"""Port 注册表与启动校验（阶段00 §3.5 / 阶段01 §3.11）。"""

from app.kernel.registry.port_registry import (
    AssemblyError,
    PortPriority,
    PortRegistration,
    PortRegistry,
    PortResolution,
    PortState,
    UnavailableCapability,
)

__all__ = [
    "AssemblyError",
    "PortPriority",
    "PortRegistration",
    "PortRegistry",
    "PortResolution",
    "PortState",
    "UnavailableCapability",
]
