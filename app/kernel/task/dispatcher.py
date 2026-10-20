from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.kernel.ports.lock_provider import LockProvider
from app.kernel.task.service import transition_task
from app.kernel.task.states import TaskStatus
from app.models import Confirmation, Conversation, Message, Outbox, Task, TaskCheckpoint, TaskEvent
from app.core.ids import new_uuid7


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class OutboxDispatcher:
    def __init__(self, queue, lock_provider: LockProvider | None = None) -> None:
        self.queue = queue
        # lock.provider Port 解析结果由装配根注入（P1-4）：认领段加互斥防多实例重复认领；
        # 单进程下语义不变。None 时退化为原行为（测试/直构场景）。
        self._lock_provider = lock_provider

    async def dispatch_once(self, db: Session) -> bool:
        now = datetime.now(timezone.utc)
        if self._lock_provider is not None:
            async with self._lock_provider.acquire("outbox.dispatch"):
                row = self._claim(db, now)
        else:
            row = self._claim(db, now)
        if row is None:
            return False
        try:
            if row.event_type == "TASK_READY":
                task = db.get(Task, row.aggregate_id)
                if task is not None and task.status == TaskStatus.PENDING:
                    transition_task(db, task, TaskStatus.QUEUED, "TASK_QUEUED", "task queued")
                    db.commit()
                if task is not None and task.status == TaskStatus.QUEUED:
                    await self.queue.put(UUID(str(row.aggregate_id)))
            db.execute(
                update(Outbox)
                .where(Outbox.id == row.id, Outbox.status == "PROCESSING")
                .values(status="SENT", next_retry_at=None, sent_at=datetime.now(timezone.utc))
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            from app.config import get_settings
            max_attempts = get_settings().task_outbox_max_attempts
            failed = row.attempt_count >= max_attempts
            db.execute(
                update(Outbox)
                .where(Outbox.id == row.id, Outbox.status == "PROCESSING")
                .values(status="FAILED" if failed else "PENDING", next_retry_at=None if failed else datetime.now(timezone.utc), last_error=str(exc)[:500])
            )
            db.commit()
        return True

    def _claim(self, db: Session, now: datetime) -> Outbox | None:
        """认领一条到期 Outbox（with_for_update 保留：生产 PG 行锁仍有效）。"""
        row = db.scalar(
            select(Outbox)
            .where(
                Outbox.status.in_(["PENDING", "PROCESSING"]),
                (Outbox.next_retry_at.is_(None)) | (Outbox.next_retry_at <= now),
            )
            .order_by(Outbox.created_at)
            .with_for_update()
            .limit(1)
        )
        if row is None:
            return None
        claimed = db.execute(
            update(Outbox)
            .where(Outbox.id == row.id, Outbox.status.in_(["PENDING", "PROCESSING"]))
            .values(
                status="PROCESSING",
                attempt_count=Outbox.attempt_count + 1,
                next_retry_at=now + timedelta(seconds=30),
            )
        )
        if claimed.rowcount != 1:
            db.rollback()
            return None
        db.commit()
        return row


def execute_simulated(db: Session, task_id: UUID) -> None:
    claimed = db.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == TaskStatus.QUEUED)
        .values(status=TaskStatus.RUNNING, attempt_count=Task.attempt_count + 1)
    )
    if claimed.rowcount != 1:
        return
    db.commit()
    task = db.get(Task, task_id)
    if task is None:
        return
    db.refresh(task)
    checkpoint = db.scalar(select(TaskCheckpoint).where(TaskCheckpoint.task_id == task.id))
    if checkpoint is not None:
        if checkpoint.status not in {"APPROVED", "RESUMED"}:
            transition_task(db, task, TaskStatus.FAILED, "TASK_CHECKPOINT_INVALID", "task checkpoint is not approved")
            task.error_code = "AI_CHECKPOINT_INVALID"
            task.error_message = "task checkpoint is not approved for resume"
            db.commit()
            return
        confirmation = db.get(Confirmation, checkpoint.confirmation_id) if checkpoint.confirmation_id else None
        if confirmation is None or confirmation.status != "APPROVED":
            transition_task(db, task, TaskStatus.FAILED, "TASK_CHECKPOINT_INVALID", "confirmation is not approved")
            task.error_code = "AI_CONFIRMATION_INVALID"
            task.error_message = "confirmation is not approved for resume"
            db.commit()
            return
        expected_hash = checkpoint.execution_context.get("pending_call_hash")
        actual_hash = hashlib.sha256(json.dumps(checkpoint.pending_call, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        if not expected_hash or expected_hash != actual_hash or not checkpoint.idempotency_key:
            transition_task(db, task, TaskStatus.FAILED, "TASK_CHECKPOINT_INVALID", "checkpoint call integrity validation failed")
            task.error_code = "AI_CHECKPOINT_INVALID"
            task.error_message = "checkpoint call integrity validation failed"
            db.commit()
            return
        if checkpoint.status == "APPROVED":
            checkpoint.resumed_at = datetime.now(timezone.utc)
            checkpoint.status = "RESUMED"
        db.flush()
    task.last_event_seq += 1
    db.add(TaskEvent(
        id=new_uuid7(),
        tenant_id=task.tenant_id, task_id=task.id, seq=task.last_event_seq,
        event_type="TASK_STARTED", from_state=TaskStatus.QUEUED,
        to_state=TaskStatus.RUNNING, summary="task started",
    ))
    db.commit()
    try:
        if task.cancel_requested_at is not None:
            if task.status == TaskStatus.RUNNING:
                transition_task(db, task, TaskStatus.CANCEL_REQUESTED, "TASK_CANCEL_REQUESTED", "task cancellation requested")
            transition_task(db, task, TaskStatus.CANCELLED, "TASK_CANCELLED", "task cancelled before execution")
            db.commit()
            return
        if _aware(task.deadline_at) is not None and _aware(task.deadline_at) <= datetime.now(timezone.utc):
            task.error_code = "AI_TASK_TIMEOUT"
            task.error_message = "task deadline exceeded"
            transition_task(db, task, TaskStatus.TIMEOUT, "TASK_TIMEOUT", "task timed out")
            db.commit()
            return
    except Exception:
        db.rollback()
        task = db.get(Task, task_id)
        if task is not None and task.status == TaskStatus.RUNNING:
            task.error_code = "AI_TASK_EXECUTION_FAILED"
            task.error_message = "simulated execution failed"
            transition_task(db, task, TaskStatus.FAILED, "TASK_FAILED", "task execution failed")
            db.commit()
        return
    task.result = {"mode": "simulated", "status": "completed"}
    if task.conversation_id is not None:
        result_message = Message(
            tenant_id=task.tenant_id,
            conversation_id=task.conversation_id,
            sender_type="ASSISTANT",
            content="[MOCK] task completed",
            status="COMPLETED",
            task_id=task.id,
        )
        db.add(result_message)
        db.flush()
        task.result_message_id = result_message.id
        conversation = db.get(Conversation, task.conversation_id)
        if conversation is not None:
            conversation.last_message_at = datetime.now(timezone.utc)
    transition_task(db, task, TaskStatus.SUCCESS, "TASK_SUCCEEDED", "task completed")
    db.commit()
