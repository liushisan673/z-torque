from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.kernel.audit.service import record_audit
from app.core.ids import new_uuid7
from app.kernel.ports.approval_provider import ApprovalProvider
from app.kernel.registry.active import call_optional
from app.kernel.task.checkpoints import latest_checkpoint
from app.kernel.task.service import transition_task
from app.kernel.task.states import TaskStatus
from app.models import Confirmation, Conversation, Message, Outbox, Task, TaskCheckpoint
from app.security.permissions import is_admin_user


class BuiltinApprovalProvider(ApprovalProvider):
    """approval.provider Port 内核默认实现：DB 人工确认队列（社区版最小实现）。

    审批请求/裁决的持久化与审计语义原地复用模块函数；审批与任务状态机的衔接
    （checkpoint / WAITING_HUMAN）属内核不变量2，仍由 pause_task_for_confirmation
    承载。企业版可经 PortRegistry 以 PLUGIN/EXPLICIT 优先级替换为审批中心。
    """

    def request(self, db: Session, *, tenant_id: UUID, requested_by_user_id: UUID, reason: str, snapshot: dict, task_id: UUID | None = None, skill_version_id: UUID | None = None, tool_version_id: UUID | None = None) -> Confirmation:
        return request_confirmation(db, tenant_id=tenant_id, requested_by_user_id=requested_by_user_id, reason=reason, snapshot=snapshot, task_id=task_id, skill_version_id=skill_version_id, tool_version_id=tool_version_id)

    def resolve(self, db: Session, *, tenant_id: UUID, confirmation_id: UUID, user_id: UUID, approved: bool) -> Confirmation:
        return resolve_confirmation(db, tenant_id=tenant_id, confirmation_id=confirmation_id, user_id=user_id, approved=approved)


def request_confirmation(db: Session, *, tenant_id: UUID, requested_by_user_id: UUID, reason: str, snapshot: dict, task_id: UUID | None = None, skill_version_id: UUID | None = None, tool_version_id: UUID | None = None) -> Confirmation:
    confirmation = Confirmation(tenant_id=tenant_id, requested_by_user_id=requested_by_user_id, reason=reason, request_snapshot=snapshot, task_id=task_id, skill_version_id=skill_version_id, tool_version_id=tool_version_id, status="PENDING")
    db.add(confirmation)
    db.flush()
    record_audit(db, tenant_id=tenant_id, actor_user_id=requested_by_user_id, action="CONFIRMATION_REQUESTED", resource_type="CONFIRMATION", resource_id=confirmation.id, summary="execution confirmation requested")
    return confirmation


def pause_task_for_confirmation(db: Session, *, tenant_id: UUID, user_id: UUID, task_id: UUID, reason: str, snapshot: dict, execution_context: dict, completed_steps: list[str], pending_steps: list[str], pending_call: dict, idempotency_key: str, skill_version_id: UUID | None = None, tool_version_id: UUID | None = None, approval_provider: ApprovalProvider | None = None) -> Confirmation:
    task = db.get(Task, task_id)
    if not task or task.tenant_id != tenant_id or task.owner_user_id != user_id:
        raise ApiError("AI_TASK_NOT_FOUND", "task not found", 404)
    if task.status != TaskStatus.RUNNING:
        raise ApiError("AI_TASK_STATE_CONFLICT", "task is not running", 409)
    existing = latest_checkpoint(db, task.id)
    if existing and existing.status == "WAITING_APPROVAL":
        confirmation = db.get(Confirmation, existing.confirmation_id) if existing.confirmation_id else None
        if confirmation:
            return confirmation
    # 审批队列经 approval.provider Port 创建（P2 收口）；审批与任务状态机的衔接仍在内核
    provider = approval_provider or BuiltinApprovalProvider()
    confirmation = provider.request(db, tenant_id=tenant_id, requested_by_user_id=user_id, reason=reason, snapshot=snapshot, task_id=task.id, skill_version_id=skill_version_id, tool_version_id=tool_version_id)
    execution_context = {**execution_context, "pending_call_hash": _call_hash(pending_call)}
    if existing is not None:
        # 同任务再次挂起（前次审批已恢复、执行循环又命中新的待确认工具）：
        # zr_task_checkpoint.task_id 唯一，必须复用原行并整体更新为新一轮挂起状态，
        # 否则 INSERT 触发 IntegrityError 使任务以 AI_TASK_FAILED 告终
        existing.status = "WAITING_APPROVAL"
        existing.execution_context = execution_context
        existing.completed_steps = completed_steps
        existing.pending_steps = pending_steps
        existing.pending_call = pending_call
        existing.idempotency_key = idempotency_key
        existing.confirmation_id = confirmation.id
        existing.resumed_at = None
    else:
        checkpoint = TaskCheckpoint(tenant_id=tenant_id, task_id=task.id, status="WAITING_APPROVAL", execution_context=execution_context, completed_steps=completed_steps, pending_steps=pending_steps, pending_call=pending_call, idempotency_key=idempotency_key, confirmation_id=confirmation.id)
        db.add(checkpoint)
    transition_task(db, task, TaskStatus.WAITING_HUMAN, "TASK_WAITING_HUMAN", "task is waiting for human approval")
    db.flush()
    # 审批待办通知经 notify.channel Port（可选能力：不可用则审计后降级，P2-14）
    call_optional(
        db,
        audit_tenant_id=tenant_id,
        port_name="notify.channel",
        method="send",
        actor_user_id=user_id,
        tenant_id=tenant_id,
        user_id=user_id,
        title="任务等待人工审批",
        body=reason,
        level="warning",
        meta={"task_id": str(task.id), "confirmation_id": str(confirmation.id)},
    )
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
    if item.requested_by_user_id == user_id and not is_admin_user(db, tenant_id=tenant_id, user_id=user_id):
        raise ApiError("AI_CONFIRMATION_SELF_APPROVAL", "requester cannot resolve own confirmation", 403)
    item.status = "APPROVED" if approved else "REJECTED"
    item.confirmed_by_user_id = user_id
    item.resolved_at = datetime.now(timezone.utc)
    record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="CONFIRMATION_RESOLVED", resource_type="CONFIRMATION", resource_id=item.id, summary="execution confirmation resolved", detail={"approved": approved})
    if item.task_id:
        task = db.get(Task, item.task_id)
        checkpoint = latest_checkpoint(db, item.task_id)
        if task and checkpoint and task.status == TaskStatus.WAITING_HUMAN:
            if approved:
                checkpoint.status = "APPROVED"
                transition_task(db, task, TaskStatus.QUEUED, "TASK_APPROVAL_GRANTED", "task approval granted")
                db.add(Outbox(id=new_uuid7(), tenant_id=task.tenant_id, aggregate_type="TASK", aggregate_id=task.id, event_type="TASK_READY", payload={"task_id": str(task.id), "resume_checkpoint_id": str(checkpoint.id)}))
            else:
                checkpoint.status = "REJECTED"
                task.error_code = "AI_CONFIRMATION_REJECTED"
                task.error_message = "用户拒绝工具执行，任务终止"
                transition_task(db, task, TaskStatus.CANCELLED, "TASK_APPROVAL_REJECTED", "task approval rejected")
                # 拒绝以模型气泡告知（对齐取消气泡模式）：会话流出现提示，前端收起执行块；
                # 消息带 task_id，保留「查看执行过程」入口
                if task.conversation_id is not None:
                    reject_text = "您已拒绝该工具执行，本次任务已终止。"
                    result_message = db.query(Message).filter(Message.task_id == task.id, Message.sender_type == "ASSISTANT").first()
                    if result_message is not None:
                        result_message.content = reject_text
                    else:
                        db.add(Message(
                            tenant_id=task.tenant_id, conversation_id=task.conversation_id, sender_type="ASSISTANT",
                            content=reject_text, status="COMPLETED", task_id=task.id,
                        ))
                    conversation = db.get(Conversation, task.conversation_id)
                    if conversation is not None:
                        conversation.last_message_at = datetime.now(timezone.utc)
    return item


def _call_hash(call: dict) -> str:
    encoded = json.dumps(call, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()
