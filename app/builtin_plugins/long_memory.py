"""memory.long Port 的内置实现：进程内 KV 长期记忆（契约 §3.5 对照表「内置简单 KV，可后续接向量」）。

单机进程内存储、进程重启即失（诚实语义）；memory_long_enabled 默认 False。
调用方：context_builder 召回（build_context）与 runtime 摘要写入（_maybe_summarize）。
企业版可经 PortRegistry 以 PLUGIN/EXPLICIT 优先级替换为 DB / 向量后端（可后续接 vector.store）。
"""

from uuid import UUID

from app.builtin_plugins.manifest import PluginManifest
from app.kernel.ports.memory_long import LongTermMemoryStore


class InProcessKvLongMemory(LongTermMemoryStore):
    def __init__(self) -> None:
        self._items: dict[tuple[UUID, UUID, str], str] = {}

    def remember(self, *, tenant_id: UUID, user_id: UUID, key: str, value: str) -> None:
        self._items[(tenant_id, user_id, key)] = value

    def recall(self, *, tenant_id: UUID, user_id: UUID, key: str) -> str | None:
        return self._items.get((tenant_id, user_id, key))

    def forget(self, *, tenant_id: UUID, user_id: UUID, key: str) -> None:
        self._items.pop((tenant_id, user_id, key), None)


class LongMemoryPlugin:
    manifest = PluginManifest(
        name="long_memory",
        description="进程内长期记忆 KV（单机、重启即失；企业版可换 DB/向量后端）",
        provides=("memory.long",),
    )

    def contribute(self, sdk) -> None:
        sdk.register_port("memory.long", InProcessKvLongMemory, label="builtin_inprocess_kv")
