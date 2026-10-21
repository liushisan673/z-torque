"""Task checkpoint 查询收口（P2-19）。

`zr_task_checkpoint.task_id` 现行迁移是唯一约束，但历史库可能残留多行；
所有"取该任务 checkpoint"的调用点统一走本模块，用确定性排序取最新一条，
避免 `db.scalar(select(...))` 在多 checkpoint 场景下取哪条不确定。
"""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TaskCheckpoint


def latest_checkpoint(db: Session, task_id: UUID, *, statuses: Iterable[str] | None = None) -> TaskCheckpoint | None:
    """取任务最新 checkpoint（created_at 倒序，id 兜底；UUIDv7 单调等价于插入顺序）。"""
    stmt = select(TaskCheckpoint).where(TaskCheckpoint.task_id == task_id)
    if statuses is not None:
        stmt = stmt.where(TaskCheckpoint.status.in_(list(statuses)))
    return db.scalar(
        stmt.order_by(TaskCheckpoint.created_at.desc(), TaskCheckpoint.id.desc()).limit(1)
    )
