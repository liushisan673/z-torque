import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.ids import new_uuid7
from app.kernel.audit.service import record_audit_detached
from app.kernel.ports.lock_provider import LockProvider
from app.kernel.security_context import SecurityContext
from app.kernel.skill_gateway import SkillGateway
from app.kernel.task.checkpoints import latest_checkpoint
from app.kernel.task.service import transition_task
from app.kernel.task.states import TERMINAL_STATES, TaskStatus
from app.models import Confirmation, Outbox, Task, TaskCheckpoint, TaskEvent

# 单机 lease：只防同进程并发与崩溃残留，不做多副本抢占。
_LEASE_TTL_SECONDS = 300
_DEFAULT_LEASE_OWNER = "execute_simulated"


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


def verify_checkpoint(db: Session, task: Task) -> TaskCheckpoint | None:
    """校验审批 checkpoint 的完整性与确认状态；无 checkpoint 返回 None。

    校验失败抛 ApiError，由调用方统一收敛为显式 FAILED，不再编造成功。
    """
    checkpoint = latest_checkpoint(db, task.id)
    if checkpoint is None:
        return None
    if checkpoint.status not in {"APPROVED", "RESUMED"}:
        raise ApiError("AI_CHECKPOINT_INVALID", "task checkpoint is not approved for resume", 409)
    confirmation = db.get(Confirmation, checkpoint.confirmation_id) if checkpoint.confirmation_id else None
    if confirmation is None or confirmation.status != "APPROVED":
        raise ApiError("AI_CONFIRMATION_INVALID", "confirmation is not approved for resume", 409)
    expected_hash = checkpoint.execution_context.get("pending_call_hash")
    actual_hash = hashlib.sha256(json.dumps(checkpoint.pending_call, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    if not expected_hash or expected_hash != actual_hash or not checkpoint.idempotency_key:
        raise ApiError("AI_CHECKPOINT_INVALID", "checkpoint call integrity validation failed", 409)
    if checkpoint.status == "APPROVED":
        checkpoint.resumed_at = datetime.now(timezone.utc)
        checkpoint.status = "RESUMED"
    db.flush()
    return checkpoint


async def execute_pending_call(
    db: Session,
    task: Task,
    checkpoint: TaskCheckpoint | None,
    *,
    gateway: SkillGateway | None = None,
    cancellation_event=None,
) -> None:
    """真实执行 checkpoint 中的 pending call；没有可用执行入口时显式失败。"""
    if checkpoint is None or gateway is None:
        raise ApiError("AI_EXECUTOR_UNAVAILABLE", "no real executor is available for pending call", 503)
    call = checkpoint.pending_call or {}
    skill_version_id = (checkpoint.execution_context or {}).get("skill_version_id")
    tool_version_id = call.get("tool_version_id")
    if not skill_version_id or not tool_version_id:
        raise ApiError("AI_CHECKPOINT_INVALID", "checkpoint does not contain an executable pending call", 409)
    result = await gateway.execute_async(
        db,
        SecurityContext(tenant_id=task.tenant_id, user_id=task.owner_user_id, trace_id=str(task.id)),
        skill_version_id=UUID(str(skill_version_id)),
        tool_version_id=UUID(str(tool_version_id)),
        arguments=call.get("arguments") or {},
        task_id=task.id,
        resume_checkpoint_id=checkpoint.id,
        cancellation_event=cancellation_event,
        deadline_at=task.deadline_at,
    )
    if result.status != "SUCCEEDED":
        raise ApiError(result.error_code or "AI_SKILL_FAILED", result.error_message or "skill execution failed", 502)
    task.result = {"skill_result": result.data}


def _fail_task(db: Session, task: Task, code: str, message: str, *, event_type: str = "TASK_FAILED") -> None:
    task.error_code = code
    task.error_message = message
    transition_task(
        db, task, TaskStatus.FAILED, event_type, message,
        expected_epoch=task.attempt_epoch,
    )


async def execute_simulated(
    db: Session,
    task_id: UUID,
    *,
    gateway: SkillGateway | None = None,
    cancellation_event=None,
    lease_owner: str = _DEFAULT_LEASE_OWNER,
) -> dict:
    """认领并执行任务：先 verify_checkpoint，再 execute_pending_call。

    历史同名函数的“校验后直接写 completed + [MOCK] + SUCCESS”假成功已移除；
    无法真实执行时收敛为 FAILED(AI_EXECUTOR_UNAVAILABLE)，不写任何成功消息。
    """
    now = datetime.now(timezone.utc)
    claimed = db.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == TaskStatus.QUEUED)
        .values(
            status=TaskStatus.RUNNING,
            started_at=now,
            attempt_count=Task.attempt_count + 1,
            attempt_epoch=Task.attempt_epoch + 1,
            lease_owner=lease_owner,
            lease_expires_at=now + timedelta(seconds=_LEASE_TTL_SECONDS),
            lease_heartbeat_at=now,
        )
    )
    if claimed.rowcount != 1:
        return {"task_id": str(task_id), "status": "SKIPPED"}
    db.commit()
    task = db.get(Task, task_id)
    if task is None:
        return {"task_id": str(task_id), "status": "NOT_FOUND"}
    db.refresh(task)
    try:
        checkpoint = verify_checkpoint(db, task)
        task.last_event_seq += 1
        db.add(TaskEvent(
            id=new_uuid7(),
            tenant_id=task.tenant_id, task_id=task.id, seq=task.last_event_seq,
            event_type="TASK_STARTED", from_state=TaskStatus.QUEUED,
            to_state=TaskStatus.RUNNING, summary="task started",
        ))
        db.commit()
        if task.cancel_requested_at is not None:
            if task.status == TaskStatus.RUNNING:
                transition_task(db, task, TaskStatus.CANCEL_REQUESTED, "TASK_CANCEL_REQUESTED", "task cancellation requested", expected_epoch=task.attempt_epoch)
            transition_task(db, task, TaskStatus.CANCELLED, "TASK_CANCELLED", "task cancelled before execution", expected_epoch=task.attempt_epoch)
            db.commit()
            return {"task_id": str(task_id), "status": "CANCELLED"}
        if _aware(task.deadline_at) is not None and _aware(task.deadline_at) <= datetime.now(timezone.utc):
            task.error_code = "AI_TASK_TIMEOUT"
            task.error_message = "task deadline exceeded"
            transition_task(db, task, TaskStatus.TIMEOUT, "TASK_TIMEOUT", "task timed out", expected_epoch=task.attempt_epoch)
            db.commit()
            return {"task_id": str(task_id), "status": "TIMEOUT"}
        await execute_pending_call(
            db, task, checkpoint,
            gateway=gateway,
            cancellation_event=cancellation_event,
        )
        transition_task(
            db, task, TaskStatus.SUCCESS, "TASK_EXECUTOR_SUCCEEDED",
            "pending call executed", expected_epoch=task.attempt_epoch,
        )
        db.commit()
        return {"task_id": str(task_id), "status": "SUCCEEDED"}
    except ApiError as exc:
        db.rollback()
        task = db.get(Task, task_id)
        if task is None or task.status in TERMINAL_STATES:
            return {"task_id": str(task_id), "status": str(task.status) if task is not None else "NOT_FOUND"}
        db.refresh(task)
        event_type = (
            "TASK_EXECUTOR_UNAVAILABLE" if exc.code == "AI_EXECUTOR_UNAVAILABLE"
            else "TASK_CHECKPOINT_INVALID" if exc.code in {"AI_CHECKPOINT_INVALID", "AI_CONFIRMATION_INVALID", "AI_SKILL_FAILED"}
            else "TASK_FAILED"
        )
        _fail_task(db, task, exc.code, exc.message, event_type=event_type)
        if exc.code == "AI_EXECUTOR_UNAVAILABLE":
            record_audit_detached(
                tenant_id=task.tenant_id,
                action="task.executor_unavailable",
                resource_type="TASK",
                resource_id=task.id,
                actor_user_id=task.owner_user_id,
                summary="task executor unavailable",
                detail={"error_code": exc.code, "message": exc.message},
                trace_id=str(task.id),
            )
        db.commit()
        return {"task_id": str(task_id), "status": "FAILED", "error_code": exc.code}
    except Exception:
        db.rollback()
        task = db.get(Task, task_id)
        if task is None or task.status in TERMINAL_STATES:
            return {"task_id": str(task_id), "status": str(task.status) if task is not None else "NOT_FOUND"}
        db.refresh(task)
        _fail_task(db, task, "AI_TASK_EXECUTION_FAILED", "task execution failed")
        db.commit()
        return {"task_id": str(task_id), "status": "FAILED", "error_code": "AI_TASK_EXECUTION_FAILED"}
