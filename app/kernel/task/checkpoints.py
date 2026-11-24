"""Task checkpoint 查询收口（P2-19）。

`zr_task_checkpoint.task_id` 现行迁移是唯一约束，但历史库可能残留多行；
所有"取该任务 checkpoint"的调用点统一走本模块，用确定性排序取最新一条，
避免 `db.scalar(select(...))` 在多 checkpoint 场景下取哪条不确定。

阶段13 起本模块还承担「决策轮种子」的存取（见 SEED_STATUS 注释）。
"""

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TaskCheckpoint

# 种子状态：/chat/stream 的决策轮结论，仅用于让 worker 跳过重复推理。
# 它不是审批 checkpoint（不带 confirmation / pending_call），
# worker 认领任务后立即取走并删除，之后才可能创建真正的审批 checkpoint
# （task_id 唯一约束要求同一任务只能有一行）。
SEED_STATUS = "SEEDED"


def latest_checkpoint(db: Session, task_id: UUID, *, statuses: Iterable[str] | None = None) -> TaskCheckpoint | None:
    """取任务最新 checkpoint（created_at 倒序，id 兜底；UUIDv7 单调等价于插入顺序）。"""
    stmt = select(TaskCheckpoint).where(TaskCheckpoint.task_id == task_id)
    if statuses is not None:
        stmt = stmt.where(TaskCheckpoint.status.in_(list(statuses)))
    return db.scalar(
        stmt.order_by(TaskCheckpoint.created_at.desc(), TaskCheckpoint.id.desc()).limit(1)
    )


def create_seed_checkpoint(db: Session, *, task_id: UUID, tenant_id: UUID, idempotency_key: str, seed: dict[str, Any]) -> TaskCheckpoint:
    """随任务创建一起写入决策轮种子（不 flush 之外的副作用，由调用方提交）。"""
    checkpoint = TaskCheckpoint(
        tenant_id=tenant_id,
        task_id=task_id,
        status=SEED_STATUS,
        execution_context={"seed": seed},
        completed_steps=[],
        pending_steps=[],
        pending_call={},
        idempotency_key=idempotency_key,
    )
    db.add(checkpoint)
    db.flush()
    return checkpoint


def take_seed_checkpoint(db: Session, task_id: UUID) -> dict[str, Any] | None:
    """取走并删除种子（仅 worker 认领任务后调用一次）。

    先删后用：即使随后执行崩溃，重放也不会因为种子残留而卡住审批 checkpoint 的唯一约束；
    取不到种子只是少一次优化，执行循环会照常从第 1 轮推理开始。
    """
    checkpoint = latest_checkpoint(db, task_id, statuses=(SEED_STATUS,))
    if checkpoint is None:
        return None
    seed = dict(checkpoint.execution_context or {}).get("seed")
    db.delete(checkpoint)
    db.flush()
    return seed if isinstance(seed, dict) else None
