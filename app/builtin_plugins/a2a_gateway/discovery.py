"""只读 Agent 发现投影（v1.3 §6.2）。

只读、租户内、仅已发布版本：暴露 `name / description / capabilities`，
**不**暴露草稿、未发布配置与提示词全文（`config_snapshot` 只取 capabilities 白名单键）。

标准插件约束：本模块不再 `from app.models import Agent, AgentVersion`，
投影 SQL 收敛在内核只读缝 `PluginSdk.read_published_agents` 之后
（`app.kernel.plugin_reads`）。插件换内核时无需跟着搬 `app/models`。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


class AgentDiscovery:
    """已发布 Agent 的只读投影（供 A2A 对端发现本地能力）。"""

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk

    def list_agents(self, *, tenant_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
        # 内核侧已完成：ACTIVE + PUBLISHED 过滤、capability 白名单投影、limit 上界收敛
        return self._sdk.read_published_agents(tenant_id=tenant_id, limit=limit)
