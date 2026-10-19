import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import ApiError
from app.core.ids import new_uuid7
from app.kernel.audit.service import record_audit
from app.kernel.model_gateway import ModelGateway, ModelGatewayError, ModelRequest
from app.kernel.ports.queue_driver import InMemoryQueueDriver, QueueDriver
from app.kernel.skill_gateway import SkillGateway
from app.kernel.task.dispatcher import OutboxDispatcher, execute_simulated
from app.kernel.task.service import append_task_event, sanitize_summary, transition_task
from app.kernel.task.states import TERMINAL_STATES, TaskStatus
from app.kernel.task.worker import Worker
from app.models import (
    AgentVersion,
    Confirmation,
    Conversation,
    Message,
    Outbox,
    SkillExecutionLog,
    SkillVersion,
    SkillVersionTool,
    Task,
    TaskCheckpoint,
    TaskEvent,
    ToolVersion,
)
from app.services.context_builder import build_agent_system_prompt, build_context
from app.services.execution_loop import AgentExecutionLoop
from app.services.tool_mapping_builder import build_tool_mappings

logger = logging.getLogger("z_torque.task_worker")


class TaskRuntime:
    def __init__(self, gateway: SkillGateway | None = None, model_gateway: ModelGateway | None = None, queue: QueueDriver | None = None, *, session_factory: Callable[[], Session]) -> None:
        # queue.driver Port 解析结果注入；未注入时保持原行为（进程内队列）
        self.queue = queue if queue is not None else InMemoryQueueDriver()
        # 会话工厂由装配根注入（阶段00 §3.8：内核不直接依赖具体 Session 实现）
        self.session_factory = session_factory
        self.dispatcher = OutboxDispatcher(self.queue)
        settings = get_settings()
        self.worker = Worker(self.queue, self._execute, concurrency=settings.worker_concurrency)
        self.gateway = gateway
        self.model_gateway = model_gateway
        self._tasks: list[asyncio.Task] = []
        self._cancellation_events: dict[UUID, asyncio.Event] = {}

    async def _execute(self, task_id: UUID) -> dict:
        try:
            return await self._execute_inner(task_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # fail-safe：执行器崩溃时尽力把任务标记为 FAILED，避免永久停留在 RUNNING
            logger.exception("task %s executor crashed", task_id)
            try:
                with self.session_factory() as db:
                    task = db.get(Task, task_id)
                    if task is not None and task.status not in TERMINAL_STATES:
                        db.rollback()
                        db.execute(
                            update(Task)
                            .where(Task.id == task_id, Task.status.in_([TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.RUNNING]))
                            .values(status=TaskStatus.FAILED, error_code="AI_TASK_FAILED", error_message="task executor crashed", finished_at=datetime.now(UTC), last_event_seq=Task.last_event_seq + 1)
                        )
                        db.add(TaskEvent(id=new_uuid7(), tenant_id=task.tenant_id, task_id=task.id, seq=task.last_event_seq + 1, event_type="TASK_FAILED", to_state=TaskStatus.FAILED, summary=sanitize_summary("task executor crashed")))
                        db.commit()
            except Exception:
                logger.exception("task %s fail-safe marking failed", task_id)
            return {"task_id": str(task_id), "status": "FAILED"}

    async def _execute_inner(self, task_id: UUID) -> dict:
        with self.session_factory() as db:
            cancel_event = self._cancellation_events.setdefault(task_id, asyncio.Event())
            task = db.get(Task, task_id)
            if task is None:
                return {"task_id": str(task_id), "status": "NOT_FOUND"}
            checkpoint = db.scalar(select(TaskCheckpoint).where(TaskCheckpoint.task_id == task_id, TaskCheckpoint.status.in_(["APPROVED", "RESUMED"])))
            if checkpoint is not None and self.gateway is not None:
                # chat 型任务（绑定会话）审批恢复走"重放工具 → 续跑执行循环 → 最终回复"；
                # skill 型任务保持原有单工具重放语义。
                if task.conversation_id is not None:
                    return await self._resume_chat_task(db, task, checkpoint, cancel_event)
                if task.status not in (TaskStatus.QUEUED, TaskStatus.RUNNING):
                    return {"task_id": str(task_id), "status": str(task.status)}
                return await self._resume_skill_task(db, task, checkpoint, cancel_event)
            if self.model_gateway is not None:
                result = await self._execute_model_task(db, task_id)
                if result is not None:
                    return result
            execute_simulated(db, task_id)
            self._cancellation_events.pop(task_id, None)
        return {"task_id": str(task_id)}

    def request_cancellation(self, task_id: UUID) -> None:
        self._cancellation_events.setdefault(task_id, asyncio.Event()).set()

    async def _claim(self, db, task_id: UUID) -> bool:
        """原子认领 QUEUED→RUNNING，并补写 TASK_STARTED 事件（claim 是裸更新，事件流需要"开始执行"步骤）。"""
        claimed = db.execute(
            update(Task).where(Task.id == task_id, Task.status == TaskStatus.QUEUED)
            .values(status=TaskStatus.RUNNING, started_at=datetime.now(UTC), attempt_count=Task.attempt_count + 1)
        )
        if claimed.rowcount != 1:
            db.rollback()
            return False
        db.commit()
        task = db.get(Task, task_id)
        db.add(TaskEvent(id=new_uuid7(), tenant_id=task.tenant_id, task_id=task.id, seq=task.last_event_seq + 1, event_type="TASK_STARTED", from_state=TaskStatus.QUEUED, to_state=TaskStatus.RUNNING, summary="task started"))
        task.last_event_seq += 1
        db.commit()
        return True

    async def _execute_model_task(self, db, task_id: UUID) -> dict | None:
        task = db.get(Task, task_id)
        if task is None or task.conversation_id is None:
            return None
        if task.status != TaskStatus.QUEUED:
            return {"task_id": str(task_id), "status": str(task.status)}
        if not await self._claim(db, task_id):
            return {"task_id": str(task_id), "status": "SKIPPED"}
        db.refresh(task)
        cancel_event = self._cancellation_events.setdefault(task_id, asyncio.Event())
        try:
            conversation = db.get(Conversation, task.conversation_id)
            trigger = db.get(Message, task.trigger_message_id) if task.trigger_message_id else None
            if conversation is None or trigger is None:
                return self._fail_model_task(db, task, "AI_TASK_CONTEXT_INVALID", "task context is invalid")
            agent_version = db.get(AgentVersion, task.agent_version_id)
            mappings, tools = build_tool_mappings(db, agent_version, task.tenant_id)
            # 与 chat 页面流式链路一致：系统提示 + 绑定 skill 执行指令
            system_prompt = build_agent_system_prompt(db, agent_version, [m.skill_version_id for m in mappings.values() if m.skill_version_id])
            request = ModelRequest(
                trace_id=str(task.id), task_id=task.id, conversation_id=conversation.id,
                agent_version_id=task.agent_version_id, model=get_settings().model_name,
                messages=build_context(db, conversation, system_prompt=system_prompt),
                tools=tools, timeout_seconds=get_settings().model_timeout_seconds,
            )
            if mappings and self.gateway is not None:
                result = await AgentExecutionLoop(self.model_gateway, self.gateway).run(
                    request=request,
                    mappings=mappings,
                    db=db,
                    tenant_id=task.tenant_id,
                    user_id=task.owner_user_id,
                    task_id=task.id,
                    cancellation_event=cancel_event,
                    deadline_at=task.deadline_at,
                )
                if result.pending_confirmation is not None:
                    # 写入类工具挂起等待审批：确认机制已把任务置为 WAITING_HUMAN，worker 直接让出，不判失败
                    return {"task_id": str(task_id), "status": "WAITING_HUMAN"}
                response = result.response
            else:
                response = await self.model_gateway.generate(request)
        except ModelGatewayError as exc:
            db.rollback()
            return self._fail_model_task(db, task, exc.code, exc.message)
        except ApiError as exc:
            db.rollback()
            return self._fail_model_task(db, task, exc.code, exc.message)
        except Exception:
            # 先回滚失败事务（如落库约束冲突），否则后续 transition 会因 PendingRollbackError 二次崩溃
            db.rollback()
            logger.exception("chat task %s failed", task_id)
            return self._fail_model_task(db, task, "AI_TASK_FAILED", "chat task failed")
        if response.tool_calls:
            return self._fail_model_task(db, task, "AI_TOOL_MAPPING_INVALID", "tool execution requires an agent tool mapping")
        if not response.content:
            return self._fail_model_task(db, task, "AI_MODEL_RESPONSE_INVALID", "model returned an empty response")
        await self._finish_chat_task(db, task, conversation, trigger, response)
        self._cancellation_events.pop(task_id, None)
        return {"task_id": str(task_id), "status": "SUCCEEDED"}

    async def _resume_chat_task(self, db, task: Task, checkpoint: TaskCheckpoint, cancel_event: asyncio.Event) -> dict:
        """chat 型任务审批恢复：重放挂起的工具调用，回填结果后继续执行循环直至最终回复。"""
        task_id = task.id
        if task.status != TaskStatus.QUEUED:
            return {"task_id": str(task_id), "status": str(task.status)}
        if not await self._claim(db, task_id):
            return {"task_id": str(task_id), "status": "SKIPPED"}
        db.refresh(task)
        chat_context = (checkpoint.execution_context or {}).get("chat_context") or {}
        snapshot = chat_context.get("messages") or []
        if not snapshot:
            return self._fail_model_task(db, task, "AI_CHECKPOINT_INVALID", "chat checkpoint context is missing")
        conversation = db.get(Conversation, task.conversation_id)
        trigger = db.get(Message, task.trigger_message_id) if task.trigger_message_id else None
        if conversation is None or trigger is None:
            return self._fail_model_task(db, task, "AI_TASK_CONTEXT_INVALID", "task context is invalid")
        agent_version = db.get(AgentVersion, task.agent_version_id)
        mappings, tools = build_tool_mappings(db, agent_version, task.tenant_id)
        system_prompt = build_agent_system_prompt(db, agent_version, [m.skill_version_id for m in mappings.values() if m.skill_version_id])
        # 重放挂起的工具调用（checkpoint 合法性与幂等检查：已成功执行过则直接取日志结果）
        call = checkpoint.pending_call
        tool_id = UUID(str(call["tool_version_id"]))
        existing = db.query(SkillExecutionLog).filter(
            SkillExecutionLog.task_id == task.id,
            SkillExecutionLog.tool_version_id == tool_id,
            SkillExecutionLog.status == "SUCCEEDED",
        ).first()
        if existing is not None:
            data = (existing.result or {}).get("data")
            tool_result = json.dumps(data, ensure_ascii=False, default=str) if data is not None else ""
        else:
            result = await self.gateway.execute_async(
                db,
                tenant_id=task.tenant_id,
                user_id=task.owner_user_id,
                skill_version_id=UUID(str(checkpoint.execution_context["skill_version_id"])),
                tool_version_id=tool_id,
                arguments=call.get("arguments", {}),
                task_id=task.id,
                resume_checkpoint_id=checkpoint.id,
                cancellation_event=cancel_event,
                deadline_at=task.deadline_at,
            )
            if result.status != "SUCCEEDED":
                if result.status == "WAITING_HUMAN":
                    return {"task_id": str(task_id), "status": "WAITING_HUMAN"}
                return self._fail_model_task(db, task, result.error_code or "AI_SKILL_FAILED", result.error_message or "tool execution failed")
            tool_result = json.dumps(result.data, ensure_ascii=False, default=str)
        request = ModelRequest(
            trace_id=str(task.id), task_id=task.id, conversation_id=conversation.id,
            agent_version_id=task.agent_version_id, model=get_settings().model_name,
            messages=[], tools=tools, timeout_seconds=get_settings().model_timeout_seconds,
        )
        try:
            loop_result = await AgentExecutionLoop(self.model_gateway, self.gateway).run(
                request=request,
                mappings=mappings,
                db=db,
                tenant_id=task.tenant_id,
                user_id=task.owner_user_id,
                task_id=task.id,
                cancellation_event=cancel_event,
                deadline_at=task.deadline_at,
                resume={"messages": snapshot, "tool_result": tool_result, "tool_call_id": chat_context.get("tool_call_id")},
            )
            if loop_result.pending_confirmation is not None:
                return {"task_id": str(task_id), "status": "WAITING_HUMAN"}
            response = loop_result.response
        except ModelGatewayError as exc:
            return self._fail_model_task(db, task, exc.code, exc.message)
        except ApiError as exc:
            return self._fail_model_task(db, task, exc.code, exc.message)
        except Exception:
            logger.exception("chat task %s resume failed", task_id)
            return self._fail_model_task(db, task, "AI_TASK_FAILED", "chat task failed")
        if not response.content:
            return self._fail_model_task(db, task, "AI_MODEL_RESPONSE_INVALID", "model returned an empty response")
        await self._finish_chat_task(db, task, conversation, trigger, response)
        self._cancellation_events.pop(task_id, None)
        return {"task_id": str(task_id), "status": "SUCCEEDED"}

    async def _finish_chat_task(self, db, task: Task, conversation: Conversation, trigger: Message | None, response) -> None:
        """chat 任务收尾：审计 + 写回复消息 + 触发消息置完成 + 任务成功 + 会话摘要维护。"""
        record_audit(
            db,
            tenant_id=task.tenant_id,
            action="MODEL_CALL_COMPLETED",
            resource_type="TASK",
            resource_id=task.id,
            actor_user_id=task.owner_user_id,
            summary="model call completed",
            detail={"provider_request_id": response.provider_request_id, "usage": response.usage.__dict__ if response.usage else None},
            trace_id=str(task.id),
        )
        result_message = db.query(Message).filter(Message.task_id == task.id, Message.sender_type == "ASSISTANT").first()
        if result_message is not None:
            # 幂等：recover 重跑或重复收尾时复用已有回复，避免 zr_message.task_id 唯一约束冲突
            result_message.content = response.content
        else:
            result_message = Message(tenant_id=task.tenant_id, conversation_id=conversation.id, sender_type="ASSISTANT", content=response.content, status="COMPLETED", task_id=task.id)
            db.add(result_message)
        db.flush()
        task.result_message_id = result_message.id
        task.result = {"content": response.content, "usage": response.usage.__dict__ if response.usage else None}
        if trigger is not None and trigger.status != "COMPLETED":
            trigger.status = "COMPLETED"
        conversation.last_message_at = datetime.now(UTC)
        transition_task(db, task, TaskStatus.SUCCESS, "TASK_MODEL_SUCCEEDED", "model task completed")
        db.commit()
        # 会话记忆维护：失败不阻断（任务已成功落库，下一条消息达到阈值时会自动重试）
        try:
            await self._maybe_summarize(db, conversation)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("summarize failed for conversation %s: %s", conversation.id, exc)

    async def _maybe_summarize(self, db, conversation: Conversation) -> None:
        from app.services.memory_service import summarize_conversation
        settings = get_settings()
        message_count = db.scalar(
            select(Message.id).where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc()).offset(settings.memory_short_summary_message_threshold - 1).limit(1)
        )
        if message_count is not None and self.model_gateway is not None:
            await summarize_conversation(db, conversation=conversation, user_id=conversation.owner_user_id, trace_id=str(conversation.id), model_gateway=self.model_gateway)

    async def _resume_skill_task(self, db, task: Task, checkpoint: TaskCheckpoint, cancel_event: asyncio.Event) -> dict:
        """skill 型任务审批恢复：校验快照有效性后重放单次工具执行（原有语义）。"""
        call = checkpoint.pending_call
        skill_id = UUID(str(checkpoint.execution_context["skill_version_id"]))
        tool_id = UUID(str(call["tool_version_id"]))
        skill = db.get(SkillVersion, skill_id)
        tool = db.get(ToolVersion, tool_id)
        link = db.query(SkillVersionTool).filter(SkillVersionTool.skill_version_id == skill_id, SkillVersionTool.tool_version_id == tool_id).first()
        if not skill or skill.tenant_id != task.tenant_id or skill.status != "PUBLISHED" or not tool or tool.tenant_id != task.tenant_id or tool.status != "PUBLISHED" or link is None:
            task.error_code = "AI_CHECKPOINT_INVALID"
            task.error_message = "checkpoint resource snapshot is no longer valid"
            transition_task(db, task, TaskStatus.FAILED, "TASK_CHECKPOINT_INVALID", "checkpoint resource snapshot is invalid")
            db.commit()
            return {"task_id": str(task.id), "status": "FAILED"}
        existing = db.query(SkillExecutionLog).filter(
            SkillExecutionLog.task_id == task.id,
            SkillExecutionLog.tool_version_id == tool_id,
            SkillExecutionLog.status == "SUCCEEDED",
        ).first()
        if existing is not None:
            task.result = existing.result or {"data": None}
            transition_task(db, task, TaskStatus.SUCCESS, "TASK_SKILL_IDEMPOTENT", "skill execution already completed")
            db.commit()
            return {"task_id": str(task.id), "status": "SUCCEEDED", "idempotent": True}
        result = await self.gateway.execute_async(
            db,
            tenant_id=task.tenant_id,
            user_id=task.owner_user_id,
            skill_version_id=skill_id,
            tool_version_id=tool_id,
            arguments=call.get("arguments", {}),
            task_id=task.id,
            resume_checkpoint_id=checkpoint.id,
            cancellation_event=cancel_event,
            deadline_at=task.deadline_at,
        )
        if result.status != "SUCCEEDED":
            task.error_code = result.error_code or "AI_SKILL_FAILED"
            task.error_message = result.error_message or "skill execution failed"
            transition_task(db, task, TaskStatus.FAILED, "TASK_SKILL_FAILED", "skill execution failed")
            db.commit()
            return {"task_id": str(task.id), "status": result.status}
        task.result = {"skill_result": result.data}
        transition_task(db, task, TaskStatus.SUCCESS, "TASK_SKILL_SUCCEEDED", "skill execution completed")
        db.commit()
        return {"task_id": str(task.id), "status": result.status}

    @staticmethod
    def _fail_model_task(db, task: Task, code: str, message: str) -> dict:
        task.error_code = code
        task.error_message = message
        # 幂等容错：重复收尾（如 recover 重放）时任务可能已是终态，跳过状态迁移只补失败气泡
        if task.status not in TERMINAL_STATES:
            transition_task(db, task, TaskStatus.FAILED, "TASK_MODEL_FAILED", "model task failed")
        # 失败以模型气泡输出：写一条 ASSISTANT 失败消息（幂等复用，避免重复收尾时唯一约束冲突）
        fail_text = f"抱歉，这次执行没有成功：{message or code}。您可以稍后重试。"
        result_message = db.query(Message).filter(Message.task_id == task.id, Message.sender_type == "ASSISTANT").first()
        if result_message is not None:
            result_message.content = fail_text
            result_message.status = "FAILED"
            result_message.error_code = code
            result_message.error_message = message
        else:
            db.add(Message(
                tenant_id=task.tenant_id, conversation_id=task.conversation_id, sender_type="ASSISTANT",
                content=fail_text, status="FAILED", task_id=task.id, error_code=code, error_message=message,
            ))
        conversation = db.get(Conversation, task.conversation_id)
        if conversation is not None:
            conversation.last_message_at = datetime.now(UTC)
        # 失败审计：与成功路径 MODEL_CALL_COMPLETED 对应，审计页可追溯失败原因
        record_audit(
            db,
            tenant_id=task.tenant_id,
            action="MODEL_CALL_FAILED",
            resource_type="TASK",
            resource_id=task.id,
            actor_user_id=task.owner_user_id,
            summary="model task failed",
            detail={"status": "failed", "error_code": code, "error_message": message},
            trace_id=str(task.id),
        )
        db.commit()
        return {"task_id": str(task.id), "status": "FAILED", "error_code": code}

    async def recover(self) -> None:
        with self.session_factory() as db:
            db.query(Outbox).filter(Outbox.status == "PROCESSING").update(
                {Outbox.status: "PENDING", Outbox.next_retry_at: None},
                synchronize_session=False,
            )
            pending = db.query(Task).filter(Task.status.in_([TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING_HUMAN, TaskStatus.CANCELING])).all()
            for task in pending:
                if task.status == TaskStatus.RUNNING:
                    transition_task(
                        db,
                        task,
                        TaskStatus.QUEUED,
                        "TASK_RECOVERED",
                        sanitize_summary("task re-queued after process restart"),
                    )
                    db.add(Outbox(
                        id=new_uuid7(),
                        tenant_id=task.tenant_id,
                        aggregate_type="TASK",
                        aggregate_id=task.id,
                        event_type="TASK_READY",
                        payload={"task_id": str(task.id), "recovered": True},
                    ))
                elif task.status == TaskStatus.CANCELING:
                    transition_task(db, task, TaskStatus.CANCELLED, "TASK_CANCELLED", "task cancelled during recovery")
                elif task.status == TaskStatus.WAITING_HUMAN:
                    checkpoint = db.scalar(select(TaskCheckpoint).where(TaskCheckpoint.task_id == task.id))
                    confirmation = db.get(Confirmation, checkpoint.confirmation_id) if checkpoint and checkpoint.confirmation_id else None
                    if confirmation and confirmation.status == "APPROVED" and checkpoint.status == "APPROVED":
                        transition_task(db, task, TaskStatus.QUEUED, "TASK_APPROVAL_RECOVERED", "approved task resumed after restart")
                        db.add(Outbox(id=new_uuid7(), tenant_id=task.tenant_id, aggregate_type="TASK", aggregate_id=task.id, event_type="TASK_READY", payload={"task_id": str(task.id), "resume_checkpoint_id": str(checkpoint.id), "recovered": True}))
            db.commit()

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                with self.session_factory() as db:
                    found = await self.dispatcher.dispatch_once(db)
                if not found:
                    await asyncio.sleep(get_settings().task_outbox_poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                # dispatcher 崩溃会导致 Outbox 停摆、任务永久停留 PENDING，必须保活
                logger.exception("task dispatcher loop failed")
                await asyncio.sleep(1.0)

    async def _timeout_loop(self) -> None:
        while True:
            try:
                with self.session_factory() as db:
                    now = datetime.now(UTC)
                    tasks = db.query(Task).filter(
                        Task.deadline_at.is_not(None),
                        Task.deadline_at <= now,
                        Task.status.in_([TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING_HUMAN, TaskStatus.CANCEL_REQUESTED]),
                    ).all()
                    for task in tasks:
                        task.error_code = "AI_TASK_TIMEOUT"
                        task.error_message = "task deadline exceeded"
                        checkpoint = db.scalar(select(TaskCheckpoint).where(TaskCheckpoint.task_id == task.id))
                        if checkpoint and checkpoint.status in {"WAITING_APPROVAL", "APPROVED"}:
                            checkpoint.status = "EXPIRED"
                        transition_task(db, task, TaskStatus.TIMEOUT, "TASK_TIMEOUT", "task timed out")
                    db.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("task timeout loop failed")
            await asyncio.sleep(0.5)

    async def start(self) -> None:
        await self.recover()
        self._tasks = [
            asyncio.create_task(self._dispatch_loop()),
            asyncio.create_task(self._timeout_loop()),
            asyncio.create_task(self.worker.run()),
        ]

    async def stop(self) -> None:
        self.worker.stop()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
