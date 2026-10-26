"""审计导出/归档的 HTTP 契约（v1.3 §5.5）。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.kernel.audit.categories import CATEGORIES, actions_for_category


class AuditExportCreateRequest(BaseModel):
    """导出请求：filters 在创建时定稿，执行阶段不可放大。"""

    format: Literal["JSONL", "CSV"] = "JSONL"
    category: str | None = None
    actions: list[str] | None = None
    actor_user_id: UUID | None = None
    resource_type: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None

    @field_validator("category")
    @classmethod
    def _known_category(cls, value: str | None) -> str | None:
        if value is not None and value not in CATEGORIES:
            raise ValueError(f"unknown category: {value}")
        return value

    def resolved_actions(self) -> tuple[str, ...] | None:
        """None = 不过滤；空元组 = 已知类别但无动作（导出 0 行，绝不退化为全量）。"""
        if self.actions is not None:
            return tuple(self.actions)
        if self.category is not None:
            return tuple(actions_for_category(self.category))
        return None


class AuditArchiveTriggerRequest(BaseModel):
    """归档触发：仅允许收窄回溯窗口，窗口上界始终为热保留边界。"""

    lookback_hours: float | None = Field(default=None, gt=0, le=24 * 365)
