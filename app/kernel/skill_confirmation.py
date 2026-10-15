from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.kernel.audit.service import record_audit
from app.core.ids import new_uuid7
from app.kernel.task.service import transition_task
from app.kernel.task.states import TaskStatus
from app.models import Confirmation, Outbox, Role, Task, TaskCheckpoint, User, user_role


def request_confirmation(db: Session, *, tenant_id: UUID, requested_by_user_id: UUID, reason: str, snapshot: dict, task_id: UUID | None = None, skill_version_id: UUID | None = None, tool_version_id: UUID | None = None) -> Confirmation:
    confirmation = Confirmation(tenant_id=tenant_id, requested_by_user_id=requested_by_user_id, reason=reason, request_snapshot=snapshot, task_id=task_id, skill_version_id=skill_version_id, tool_version_id=tool_version_id, status="PENDING")
    db.add(confirmation)
    db.flush()
    record_audit(db, tenant_id=tenant_id, actor_user_id=requested_by_user_id, action="CONFIRMATION_REQUESTED", resource_type="CONFIRMATION", resource_id=confirmation.id, summary="execution confirmation requested")
    return confirmation


def pause_task_for_confirmation(db: Session, *, tenant_id: UUID, user_id: UUID, task_id: UUID, reason: str, snapshot: dict, execution_context: dict, completed_steps: list[str], pending_steps: list[str], pending_call: dict, idempotency_key: str, skill_version_id: UUID | None = None, tool_version_id: UUID | None = None) -> Confirmation:
    task = db.get(Task, task_id)
    if not task or task.tenant_id != tenant_id or task.owner_user_id != user_id:
        raise ApiError("AI_TASK_NOT_FOUND", "task not found", 404)
    if task.status != TaskStatus.RUNNING:
        raise ApiError("AI_TASK_STATE_CONFLICT", "task is not running", 409)
    existing = db.scalar(select(TaskCheckpoint).where(TaskCheckpoint.task_id == task.id))
    if existing and existing.status == "WAITING_APPROVAL":
        confirmation = db.get(Confirmation, existing.confirmation_id) if existing.confirmation_id else None
        if confirmation:
            return confirmation
    confirmation = request_confirmation(db, tenant_id=tenant_id, requested_by_user_id=user_id, reason=reason, snapshot=snapshot, task_id=task.id, skill_version_id=skill_version_id, tool_version_id=tool_version_id)
    execution_context = {**execution_context, "pending_call_hash": _call_hash(pending_call)}
    checkpoint = TaskCheckpoint(tenant_id=tenant_id, task_id=task.id, status="WAITING_APPROVAL", execution_context=execution_context, completed_steps=completed_steps, pending_steps=pending_steps, pending_call=pending_call, idempotency_key=idempotency_key, confirmation_id=confirmation.id)
    db.add(checkpoint)
    transition_task(db, task, TaskStatus.WAITING_HUMAN, "TASK_WAITING_HUMAN", "task is waiting for human approval")
    db.flush()
    return confirmation


def resolve_confirmation(db: Session, *, tenant_id: UUID, confirmation_id: UUID, user_id: UUID, approved: bool) -> Confirmation:
    item = db.scalar(select(Confirmation).where(Confirmation.id == confirmation_id, Confirmation.tenant_id == tenant_id))
    if not item:
        raise ApiError("AI_CONFIRMATION_NOT_FOUND", "confirmation not found", 404)
    if item.status != "PENDING":
        desired = "APPROVED" if approved else "REJECTED"
        if item.status == desired:
            return item
        raise ApiError("AI_REQUEST_INVALID", "confirmation is already resolved", 422)
    if item.requested_by_user_id == user_id and not _is_admin(db, user_id):
        raise ApiError("AI_CONFIRMATION_SELF_APPROVAL", "requester cannot resolve own confirmation", 403)
    item.status = "APPROVED" if approved else "REJECTED"
    item.confirmed_by_user_id = user_id
    item.resolved_at = datetime.now(timezone.utc)
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="CONFIRMATION_RESOLVED", resource_type="CONFIRMATION", resource_id=item.id, summary="execution confirmation resolved", detail={"approved": approved})
    if item.task_id:
        task = db.get(Task, item.task_id)
        checkpoint = db.scalar(select(TaskCheckpoint).where(TaskCheckpoint.task_id == item.task_id))
        if task and checkpoint and task.status == TaskStatus.WAITING_HUMAN:
            if approved:
                checkpoint.status = "APPROVED"
                transition_task(db, task, TaskStatus.QUEUED, "TASK_APPROVAL_GRANTED", "task approval granted")
                db.add(Outbox(id=new_uuid7(), tenant_id=task.tenant_id, aggregate_type="TASK", aggregate_id=task.id, event_type="TASK_READY", payload={"task_id": str(task.id), "resume_checkpoint_id": str(checkpoint.id)}))
            else:
                checkpoint.status = "REJECTED"
                task.error_code = "AI_CONFIRMATION_REJECTED"
                task.error_message = "human approval was rejected"
                transition_task(db, task, TaskStatus.CANCELLED, "TASK_APPROVAL_REJECTED", "task approval rejected")
    return item


def _is_admin(db: Session, user_id: UUID) -> bool:
    return db.query(User).join(user_role, user_role.c.user_id == User.id).join(Role, Role.id == user_role.c.role_id).filter(User.id == user_id, Role.name == "admin").first() is not None


def _call_hash(call: dict) -> str:
    encoded = json.dumps(call, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()
