from collections import OrderedDict
from threading import RLock
from typing import Any
from uuid import UUID


class WorkingMemory:
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