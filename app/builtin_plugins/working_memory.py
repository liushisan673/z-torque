"""memory.working Port 的内置实现：进程内会话工作记忆（契约 §3.5 对照表「请求级」）。

调用方：context_builder.build_context 以会话为键缓存上下文快照（按最后一条消息 /
摘要版本号失效）。容量取 settings.memory_working_cache_size。企业版可经 PortRegistry
以 PLUGIN/EXPLICIT 优先级替换为 Redis 等共享后端。
"""

from collections import OrderedDict
from threading import RLock
from typing import Any
from uuid import UUID

from app.builtin_plugins.manifest import PluginManifest
from app.kernel.ports.memory_working import WorkingMemoryStore
from app.kernel.registry.port_registry import PortPriority


class InProcessWorkingMemory(WorkingMemoryStore):
    def __init__(self, max_entries: int = 256) -> None:
        self._items: OrderedDict[tuple[UUID, UUID, UUID], dict[str, Any]] = OrderedDict()
        self._max_entries = max_entries
        self._lock = RLock()

    def put(self, tenant_id: UUID, user_id: UUID, conversation_id: UUID, value: dict[str, Any]) -> None:
        key = (tenant_id, user_id, conversation_id)
        with self._lock:
            self._items[key] = dict(value)
            self._items.move_to_end(key)
            while len(self._items) > self._max_entries:
                self._items.popitem(last=False)

    def get(self, tenant_id: UUID, user_id: UUID, conversation_id: UUID) -> dict[str, Any] | None:
        with self._lock:
            value = self._items.get((tenant_id, user_id, conversation_id))
            return dict(value) if value else None

    def delete(self, tenant_id: UUID, user_id: UUID, conversation_id: UUID) -> None:
        with self._lock:
            self._items.pop((tenant_id, user_id, conversation_id), None)


class WorkingMemoryPlugin:
    manifest = PluginManifest(
        name="working_memory",
        description="进程内会话工作记忆（LRU，容量 memory_working_cache_size）",
        provides=("memory.working",),
    )

    def contribute(self, sdk) -> None:
        from app.config import get_settings

        max_entries = get_settings().memory_working_cache_size
        sdk.register_port("memory.working", lambda: InProcessWorkingMemory(max_entries=max_entries), label="builtin_inprocess_working_memory")
