import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import ApiError
from app.core.ids import new_uuid7
from app.core.redaction import redact_mapping
from app.core.servertime import ensure_utc
from app.kernel.audit.service import record_audit
from app.kernel.model_gateway import ModelGateway, ModelGatewayError, ModelMessage, ModelRequest
from app.kernel.ports.lock_provider import LockProvider
from app.kernel.ports.memory_short import ShortTermMemoryProvider
from app.kernel.ports.model_selector import ModelSelection, ModelSelector, resolve_model_selection
from app.kernel.ports.queue_driver import QueueDriver
from app.kernel.registry.active import call_optional, emit_event
from app.kernel.security_context import SecurityContext
from app.kernel.skill_gateway import SkillGateway
from app.kernel.task.checkpoints import latest_checkpoint
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
from app.services.context_builder import assistant_reasoning_parts, build_agent_system_prompt, build_context
from app.services.execution_loop import AgentExecutionLoop
from app.services.model_limits import effective_max_tokens
from app.services.tool_mapping_builder import build_tool_mappings

logger = logging.getLogger("z_torque.task_worker")


def _duration_ms(started_at: datetime | None) -> int | None:
    """任务耗时（毫秒，非负兜底）。

    SQLite 读回的 started_at 是 naive UTC，直接与 aware 的 now(UTC) 相减会抛
    TypeError（恢复任务收尾路径必现），因此先归一化再加非负保护。
    """
    if started_at is None:
        return None
    return max(0, int((datetime.now(UTC) - ensure_utc(started_at)).total_seconds() * 1000))


def _pending_call_matches(log: SkillExecutionLog, call: dict) -> bool:
    """审批恢复的幂等判定：必须是「同一次调用」才复用历史结果（P1-6）。

    仅按 (task_id, tool_version_id, status=SUCCEEDED) 命中会把首次调用的成功结果
    当成第二次（参数不同、同样需要审批）的结果，从而跳过用户已批准的执行。
    执行日志落库时保存了脱敏后的入参，这里比对同一口径的入参摘要。
    """
    stored = (log.result or {}).get("input") or {}
    return stored == redact_mapping(call.get("arguments") or {})


def write_task_cancel_notice(db: Session, task: Task) -> None:
    """取消以模型气泡告知（对齐失败气泡模式）：会话流出现"任务已取消"提示，
    前端据终态事件收起执行块；消息带 task_id，保留「查看执行过程」入口。
    取消路由（PENDING/QUEUED/WAITING_HUMAN）与 worker 收敛路径共用。
    """
    if task.conversation_id is None:
        return
    cancel_text = "好的，已按你的要求取消该任务，执行过程已终止。"
    result_message = db.query(Message).filter(Message.task_id == task.id, Message.sender_type == "ASSISTANT").first()
    if result_message is not None:
        result_message.content = cancel_text
    else:
        db.add(Message(
            tenant_id=task.tenant_id, conversation_id=task.conversation_id, sender_type="ASSISTANT",
            content=cancel_text, status="COMPLETED", task_id=task.id,
        ))
    conversation = db.get(Conversation, task.conversation_id)
    if conversation is not None:
        conversation.last_message_at = datetime.now(UTC)


class TaskRuntime:
    def __init__(self, gateway: SkillGateway | None = None, model_gateway: ModelGateway | None = None, *, queue: QueueDriver, session_factory: Callable[[], Session], model_selector: ModelSelector | None = None, lock_provider: LockProvider | None = None, short_memory: ShortTermMemoryProvider | None = None) -> None:
        # queue.driver Port 解析结果由装配根注入（registry.get("queue.driver")）；实现见 builtin_plugins/queue_driver.py（G2d），
        # 内核仅依赖 QueueDriver 契约，无兜底默认——队列缺失属装配失败，应启动期暴露
        self.queue = queue
        # model.selector Port 解析结果由装配根注入（文档 §6.1：ModelGateway 调用前按 AgentVersion 快照选模型）
        self.model_selector = model_selector
        # memory.short Port 解析结果由装配根注入（P2 收口）：任务成功后的会话摘要维护；
        # 未注入（直构/恢复场景）时跳过摘要并告警，不阻断任务执行
        self._short_memory = short_memory
        # 会话工厂由装配根注入（阶段00 §3.8：内核不直接依赖具体 Session 实现）
        self.session_factory = session_factory
        self.dispatcher = OutboxDispatcher(self.queue, lock_provider=lock_provider)
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
            # 强制取消兜底（cancel 路由 → worker.cancel_running）：把任务收敛为 CANCELLED，
            # 避免协程被杀后任务永久停留在 RUNNING；执行中的 DB 写由 rollback 丢弃
            try:
                with self.session_factory() as db:
                    task = db.get(Task, task_id)
                    if task is not None and task.status not in TERMINAL_STATES:
                        db.rollback()
                        # 强制取消也写提示气泡：与协作式取消收敛路径一致的会话反馈
                        write_task_cancel_notice(db, task)
                        db.execute(
                            update(Task)
                            .where(Task.id == task_id, Task.status.in_([TaskStatus.RUNNING, TaskStatus.CANCELING, TaskStatus.QUEUED]))
                            .values(status=TaskStatus.CANCELLED, error_code="AI_TASK_CANCELLED", error_message="已按用户要求取消", finished_at=datetime.now(UTC), last_event_seq=Task.last_event_seq + 1)
                        )
                        db.add(TaskEvent(id=new_uuid7(), tenant_id=task.tenant_id, task_id=task.id, seq=task.last_event_seq + 1, event_type="TASK_CANCELLED", to_state=TaskStatus.CANCELLED, summary=sanitize_summary("task cancelled")))
                        db.commit()
            except Exception:
                logger.exception("task %s cancel-safe marking failed", task_id)
            return {"task_id": str(task_id), "status": "CANCELLED"}
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
        finally:
            # 任务执行结束（终态/挂起/跳过）后回收协作式取消事件，避免字典随任务数无界增长；
            # WAITING_HUMAN 恢复重跑时 setdefault 会重新创建，取消路由只对 RUNNING 任务置位
            self._cancellation_events.pop(task_id, None)

    async def _execute_inner(self, task_id: UUID) -> dict:
        with self.session_factory() as db:
            cancel_event = self._cancellation_events.setdefault(task_id, asyncio.Event())
            task = db.get(Task, task_id)
            if task is None:
                return {"task_id": str(task_id), "status": "NOT_FOUND"}
            checkpoint = latest_checkpoint(db, task_id, statuses=("APPROVED", "RESUMED"))
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
        # 立即取消兜底：协作式取消依赖执行体在 await 边界检查事件；执行体挂死（如模型
        # 调用永不返回）时事件永远不被检查——直接 cancel 对应协程，_execute 的
        # CancelledError 分支会把任务收敛为 CANCELLED
        self.worker.cancel_running(task_id)

    def _resolve_selection(self, db: Session, tenant_id: UUID, agent_version: AgentVersion | None) -> ModelSelection:
        """model.selector 消费：AgentVersion 快照绑定优先（快照语义），无绑定/无选择器回退全局默认。

        调用参数（temperature / max_tokens）随同一快照解析：快照键优先，未设置回退
        模型档案 max_output_tokens（模型页配置），再退全局兜底。
        """
        snapshot = (agent_version.config_snapshot or {}) if agent_version is not None else {}
        settings = get_settings()
        selection = resolve_model_selection(
            self.model_selector,
            agent_version_id=agent_version.id if agent_version is not None else None,
            snapshot_model=snapshot.get("model"),
            snapshot_temperature=snapshot.get("temperature"),
            snapshot_max_tokens=snapshot.get("max_tokens"),
            default_model=settings.model_name,
            default_temperature=settings.model_temperature,
            default_max_tokens=settings.model_max_tokens,
        )
        max_tokens = effective_max_tokens(
            db,
            tenant_id=tenant_id,
            model_identifier=selection.model,
            snapshot_max_tokens=snapshot.get("max_tokens"),
            default_max_tokens=settings.model_max_tokens,
        )
        return replace(selection, max_tokens=max_tokens)

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

    def _cancel_running_task(self, db, task_id: UUID) -> dict:
        """协作式取消收尾：任务行已由取消路由置 CANCELING，终态收敛为 CANCELLED 而非 FAILED。

        竞态兜底：取消事件先于路由提交被感知时任务仍为 RUNNING，先入 CANCELING 再收敛。
        用户主动取消不写失败气泡、不落 MODEL_CALL_FAILED，仅审计 TASK_CANCELLED。
        """
        task = db.get(Task, task_id)
        if task is None:
            return {"task_id": str(task_id), "status": "NOT_FOUND"}
        if task.status == TaskStatus.RUNNING:
            transition_task(db, task, TaskStatus.CANCELING, "TASK_CANCEL_REQUESTED", "task cancellation requested")
        if task.status == TaskStatus.CANCELING:
            transition_task(db, task, TaskStatus.CANCELLED, "TASK_CANCELLED", "task cancelled")
            # 取消以模型气泡告知：会话流出现"任务已取消"提示，前端据终态事件收起执行块
            write_task_cancel_notice(db, task)
            record_audit(
                db,
                tenant_id=task.tenant_id,
                action="TASK_CANCELLED",
                resource_type="TASK",
                resource_id=task.id,
                actor_user_id=task.owner_user_id,
                summary="task cancelled by user",
                trace_id=str(task.id),
            )
            db.commit()
            return {"task_id": str(task_id), "status": "CANCELLED"}
        return {"task_id": str(task_id), "status": str(task.status)}

    async def _execute_model_task(self, db, task_id: UUID) -> dict | None:
        task = db.get(Task, task_id)
        if task is None:
            return None
        if task.status != TaskStatus.QUEUED:
            return {"task_id": str(task_id), "status": str(task.status)}
        if not await self._claim(db, task_id):
            return {"task_id": str(task_id), "status": "SKIPPED"}
        db.refresh(task)
        cancel_event = self._cancellation_events.setdefault(task_id, asyncio.Event())
        try:
            # 无会话任务（POST /tasks 未绑定会话）同样进入真实模型执行：上下文取 request_summary，
            # 不再退化为模拟执行（此前该分支使任务接口形同虚设）
            conversation = db.get(Conversation, task.conversation_id) if task.conversation_id is not None else None
            trigger = db.get(Message, task.trigger_message_id) if task.trigger_message_id else None
            if task.conversation_id is not None and (conversation is None or trigger is None):
                return self._fail_model_task(db, task, "AI_TASK_CONTEXT_INVALID", "task context is invalid")
            agent_version = db.get(AgentVersion, task.agent_version_id)
            mappings, tools = build_tool_mappings(db, agent_version, task.tenant_id)
            # 与 chat 页面流式链路一致：系统提示 + 绑定 skill 执行指令
            system_prompt = build_agent_system_prompt(db, agent_version, [m.skill_version_id for m in mappings.values() if m.skill_version_id])
            selection = self._resolve_selection(db, task.tenant_id, agent_version)
            messages = build_context(db, conversation, system_prompt=system_prompt) if conversation is not None else [ModelMessage(role="user", content=task.request_summary or "")]
            request = ModelRequest(
                trace_id=str(task.id), task_id=task.id, conversation_id=task.conversation_id,
                agent_version_id=task.agent_version_id,
                model=selection.model, temperature=selection.temperature, max_tokens=selection.max_tokens,
                messages=messages,
                tools=tools, timeout_seconds=get_settings().model_timeout_seconds,
            )
            if mappings and self.gateway is not None:
                # 不变量1：Worker 侧从持久化 Task 重建 SecurityContext（身份冻结进任务行，执行期不漂移）
                ctx = SecurityContext(tenant_id=task.tenant_id, user_id=task.owner_user_id, trace_id=str(task.id))
                result = await AgentExecutionLoop(self.model_gateway, self.gateway, self.model_selector).run(
                    request=request,
                    mappings=mappings,
                    db=db,
                    ctx=ctx,
                    task_id=task.id,
                    cancellation_event=cancel_event,
                    deadline_at=task.deadline_at,
                )
                if result.pending_confirmation is not None:
                    # 写入类工具挂起等待审批：确认机制已把任务置为 WAITING_HUMAN，worker 直接让出，不判失败。
                    # 必须显式提交：checkpoint/confirmation/WAITING_HUMAN 状态与事件都在本事务里，
                    # 不提交则 with 退出即回滚——任务回到 RUNNING、前端永远等不到确认信号（历史 bug 根因）
                    db.commit()
                    return {"task_id": str(task_id), "status": "WAITING_HUMAN"}
                response = result.response
            else:
                response = await self.model_gateway.generate(request)
        except ModelGatewayError as exc:
            db.rollback()
            return self._fail_model_task(db, task, exc.code, exc.message)
        except ApiError as exc:
            db.rollback()
            if exc.code == "AI_TASK_NOT_CANCELABLE":
                return self._cancel_running_task(db, task_id)
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
        if existing is not None and _pending_call_matches(existing, call):
            data = (existing.result or {}).get("data")
            tool_result = json.dumps(data, ensure_ascii=False, default=str) if data is not None else ""
        else:
            ctx = SecurityContext(tenant_id=task.tenant_id, user_id=task.owner_user_id, trace_id=str(task.id))
            result = await self.gateway.execute_async(
                db,
                ctx,
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
                    # 重放仍要求确认（防御分支）：持久化当前状态后让出
                    db.commit()
                    return {"task_id": str(task_id), "status": "WAITING_HUMAN"}
                return self._fail_model_task(db, task, result.error_code or "AI_SKILL_FAILED", result.error_message or "tool execution failed")
            tool_result = json.dumps(result.data, ensure_ascii=False, default=str)
        selection = self._resolve_selection(db, task.tenant_id, agent_version)
        request = ModelRequest(
            trace_id=str(task.id), task_id=task.id, conversation_id=conversation.id,
            agent_version_id=task.agent_version_id,
            model=selection.model, temperature=selection.temperature, max_tokens=selection.max_tokens,
            messages=[], tools=tools, timeout_seconds=get_settings().model_timeout_seconds,
        )
        try:
            loop_ctx = SecurityContext(tenant_id=task.tenant_id, user_id=task.owner_user_id, trace_id=str(task.id))
            loop_result = await AgentExecutionLoop(self.model_gateway, self.gateway, self.model_selector).run(
                request=request,
                mappings=mappings,
                db=db,
                ctx=loop_ctx,
                task_id=task.id,
                cancellation_event=cancel_event,
                deadline_at=task.deadline_at,
                resume={"messages": snapshot, "tool_result": tool_result, "tool_call_id": chat_context.get("tool_call_id")},
            )
            if loop_result.pending_confirmation is not None:
                # 续跑循环命中新的待确认工具：同主执行路径，先提交挂起状态再让出
                db.commit()
                return {"task_id": str(task_id), "status": "WAITING_HUMAN"}
            response = loop_result.response
        except ModelGatewayError as exc:
            db.rollback()
            return self._fail_model_task(db, task, exc.code, exc.message)
        except ApiError as exc:
            db.rollback()
            if exc.code == "AI_TASK_NOT_CANCELABLE":
                return self._cancel_running_task(db, task_id)
            return self._fail_model_task(db, task, exc.code, exc.message)
        except Exception:
            db.rollback()
            logger.exception("chat task %s resume failed", task_id)
            return self._fail_model_task(db, task, "AI_TASK_FAILED", "chat task failed")
        if not response.content:
            return self._fail_model_task(db, task, "AI_MODEL_RESPONSE_INVALID", "model returned an empty response")
        await self._finish_chat_task(db, task, conversation, trigger, response)
        self._cancellation_events.pop(task_id, None)
        return {"task_id": str(task_id), "status": "SUCCEEDED"}

    async def _finish_chat_task(self, db, task: Task, conversation: Conversation | None, trigger: Message | None, response) -> None:
        """任务收尾：审计 + 写回复消息 + 触发消息置完成 + 任务成功 + 会话摘要维护。

        conversation 为 None 表示无会话任务（POST /tasks）：结果只落 task.result，
        不写消息、不更新会话、不触发会话摘要。
        """
        record_audit(
            db,
            tenant_id=task.tenant_id,
            action="MODEL_CALL_COMPLETED",
            resource_type="TASK",
            resource_id=task.id,
            actor_user_id=task.owner_user_id,
            summary="model call completed",
            detail={
                "provider_request_id": response.provider_request_id,
                "usage": response.usage.__dict__ if response.usage else None,
                # 耗时 = 任务认领（started_at）→ 本次调用结束，供审计页"耗时"列展示
                "duration_ms": _duration_ms(task.started_at),
            },
            trace_id=str(task.id),
        )
        if conversation is not None:
            result_message = db.query(Message).filter(Message.task_id == task.id, Message.sender_type == "ASSISTANT").first()
            reasoning_parts = assistant_reasoning_parts(response.reasoning_content)
            if result_message is not None:
                # 幂等：recover 重跑或重复收尾时复用已有回复，避免 zr_message.task_id 唯一约束冲突
                result_message.content = response.content
                if reasoning_parts is not None:
                    result_message.content_parts = reasoning_parts
            else:
                result_message = Message(tenant_id=task.tenant_id, conversation_id=conversation.id, sender_type="ASSISTANT", content=response.content, content_parts=reasoning_parts, status="COMPLETED", task_id=task.id)
                db.add(result_message)
            db.flush()
            task.result_message_id = result_message.id
        task.result = {"content": response.content, "usage": response.usage.__dict__ if response.usage else None}
        if trigger is not None and trigger.status != "COMPLETED":
            trigger.status = "COMPLETED"
        if conversation is not None:
            conversation.last_message_at = datetime.now(UTC)
        transition_task(db, task, TaskStatus.SUCCESS, "TASK_MODEL_SUCCEEDED", "model task completed")
        db.commit()
        if conversation is None:
            return
        # 会话记忆维护：失败不阻断（任务已成功落库，下一条消息达到阈值时会自动重试）
        try:
            await self._maybe_summarize(db, conversation)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("summarize failed for conversation %s: %s", conversation.id, exc)

    async def _maybe_summarize(self, db, conversation: Conversation) -> None:
        settings = get_settings()
        message_count = db.scalar(
            select(Message.id).where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc()).offset(settings.memory_short_summary_message_threshold - 1).limit(1)
        )
        if message_count is None or self.model_gateway is None:
            return
        if self._short_memory is None:
            logger.warning("memory.short provider is not injected; skip post-task summarization for conversation %s", conversation.id)
            return
        item = await self._short_memory.summarize(db, conversation=conversation, user_id=conversation.owner_user_id, trace_id=str(conversation.id), model_gateway=self.model_gateway)
        # 长期记忆（memory.long Port）：开关打开时把摘要写入用户级长期 KV，供后续会话召回（P2-14）
        if settings.memory_long_enabled and item is not None:
            call_optional(
                db,
                audit_tenant_id=conversation.tenant_id,
                port_name="memory.long",
                method="remember",
                actor_user_id=conversation.owner_user_id,
                trace_id=str(conversation.id),
                tenant_id=conversation.tenant_id,
                user_id=conversation.owner_user_id,
                key=f"conversation:{conversation.id}:summary",
                value=item.summary,
            )

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
        if existing is not None and _pending_call_matches(existing, call):
            task.result = existing.result or {"data": None}
            transition_task(db, task, TaskStatus.SUCCESS, "TASK_SKILL_IDEMPOTENT", "skill execution already completed")
            db.commit()
            return {"task_id": str(task.id), "status": "SUCCEEDED", "idempotent": True}
        result = await self.gateway.execute_async(
            db,
            SecurityContext(tenant_id=task.tenant_id, user_id=task.owner_user_id, trace_id=str(task.id)),
            skill_version_id=skill_id,
            tool_version_id=tool_id,
            arguments=call.get("arguments", {}),
            task_id=task.id,
            resume_checkpoint_id=checkpoint.id,
            cancellation_event=cancel_event,
            deadline_at=task.deadline_at,
        )
        if result.status == "CANCELLED":
            # 协作式取消：终态收敛为 CANCELLED（经 _cancel_running_task 统一处理 RUNNING/CANCELING）
            return self._cancel_running_task(db, task.id)
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
        # 失败以模型气泡输出：写一条 ASSISTANT 失败消息（幂等复用，避免重复收尾时唯一约束冲突）；
        # 无会话任务没有可挂载的消息载体，失败原因只落在 task.error_code / error_message
        if task.conversation_id is not None:
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
            detail={
                "status": "failed",
                "error_code": code,
                "error_message": message,
                # 耗时 = 任务认领（started_at）→ 失败收敛，供审计页"耗时"列展示
                "duration_ms": _duration_ms(task.started_at),
            },
            trace_id=str(task.id),
        )
        # 失败通知经 notify.channel Port；能力不可用时由 call_optional 落审计并降级（P2-14）
        call_optional(
            db,
            audit_tenant_id=task.tenant_id,
            port_name="notify.channel",
            method="send",
            actor_user_id=task.owner_user_id,
            trace_id=str(task.id),
            tenant_id=task.tenant_id,
            user_id=task.owner_user_id,
            title="任务执行失败",
            body=f"{code}: {message}",
            level="error",
            meta={"task_id": str(task.id)},
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
                elif task.status in (TaskStatus.PENDING, TaskStatus.QUEUED):
                    # 进程内队列随进程重启清空，已 SENT 的 Outbox 不会重投；
                    # 必须补一条待派发记录，否则任务永久停留 PENDING/QUEUED（不变量 2）。
                    outstanding = db.query(Outbox).filter(
                        Outbox.aggregate_type == "TASK",
                        Outbox.aggregate_id == task.id,
                        Outbox.event_type == "TASK_READY",
                        Outbox.status.in_(["PENDING", "PROCESSING"]),
                    ).first()
                    if outstanding is None:
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
                    checkpoint = latest_checkpoint(db, task.id)
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
                        try:
                            # 每个任务单独 SAVEPOINT：迁移失败只回滚该任务自身的
                            # error_code / checkpoint 变更，同批其他任务与调用方已 flush
                            # 的写入（审计/事件）不受影响（P2-16）。
                            with db.begin_nested():
                                task.error_code = "AI_TASK_TIMEOUT"
                                task.error_message = "task deadline exceeded"
                                checkpoint = latest_checkpoint(db, task.id)
                                if checkpoint and checkpoint.status in {"WAITING_APPROVAL", "APPROVED"}:
                                    checkpoint.status = "EXPIRED"
                                transition_task(db, task, TaskStatus.TIMEOUT, "TASK_TIMEOUT", "task timed out")
                        except ApiError:
                            # 单个任务迁移失败（如并发已终态）不得拖垮整批超时处理
                            logger.warning("task %s timeout transition skipped: state changed concurrently", task.id)
                            continue
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
