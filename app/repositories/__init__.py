"""数据访问收口层（Phase 2 §四：属主过滤）。

`ownership` 提供单机内跨用户 / 跨 agent 的属主判定，服务层禁止裸 db.get(id)。
"""

from app.repositories.ownership import (
    get_owned,
    get_owned_version,
    is_visible,
    visible_owner_clause,
)

__all__ = ["get_owned", "get_owned_version", "is_visible", "visible_owner_clause"]
