"""模型档案运行时参数兜底（模型管理页配置生效链路）。

max_tokens 优先级：智能体版本快照（显式逐智能体配置）> 模型档案 max_output_tokens
（模型页配置，按租户 + model_identifier 匹配 ENABLED 模型）> 全局 settings.model_max_tokens。
模型页未配置或无匹配档案时原值直通，不改变既有行为。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ModelResource


def effective_max_tokens(
    db: Session,
    *,
    tenant_id: UUID,
    model_identifier: str | None,
    snapshot_max_tokens: int | None,
    default_max_tokens: int | None,
) -> int | None:
    """解析生效 max_tokens：快照显式值 > 模型档案上限（>0 才视为已配置）> 全局默认。"""
    if snapshot_max_tokens is not None:
        return snapshot_max_tokens
    if model_identifier:
        limit = db.scalar(
            select(ModelResource.max_output_tokens)
            .where(
                ModelResource.tenant_id == tenant_id,
                ModelResource.model_identifier == model_identifier,
                ModelResource.status == "ENABLED",
                ModelResource.max_output_tokens > 0,
            )
            .order_by(ModelResource.created_at)
            .limit(1)
        )
        if limit is not None:
            return int(limit)
    return default_max_tokens
