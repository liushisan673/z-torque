import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update

from app.config import get_settings
from app.core.ids import new_uuid7
from app.db.session import SessionLocal
from app.kernel.audit.service import record_audit
from app.kernel.model_gateway import ModelGateway, ModelGatewayError, ModelRequest
from app.kernel.ports.queue_driver import QueueDriver
from app.kernel.skill_gateway import SkillGateway
from app.kernel.task.dispatcher import OutboxDispatcher, execute_simulated
from app.kernel.task.service import sanitize_summary, transition_task
from app.kernel.task.states import TaskStatus
from app.kernel.task.worker import Worker
from app.models import (
    Agent,
    AgentVersion,
    AgentVersionSkill,
    Confirmation,
    Conversation,
    Message,
    Outbox,
    SkillExecutionLog,
    Skill,
    SkillVersion,
    SkillVersionTool,
    Task,
    TaskCheckpoint,
    Tool,
    ToolVersion,
)
from app.services.context_builder import build_context
from app.services.execution_loop import AgentExecutionLoop
from app.services.function_calling import ToolMapping, safe_function_name


class TaskRuntime:
    def __init__(self, gateway: SkillGateway | None = None, model_gateway: ModelGateway | None = None) -> None:
        self.queue = QueueDriver()
        self.dispatcher = OutboxDispatcher(self.queue)
        settings = get_settings()
        self.worker = Worker(self.queue, self._execute, concurrency=settings.worker_concurrency)
        self.gateway = gateway
        self.model_gateway = model_gateway
        self._tasks: list[asyncio.Task] = []
        self._cancellation_events: dict[UUID, asyncio.Event] = {}

    async def _execute(self, task_id: UUID) -> dict:
        with SessionLocal() as db:
            cancel_event = self._cancellation_events.setdefault(task_id, asyncio.Event())
            checkpoint = db.scalar(select(TaskCheckpoint).where(TaskCheckpoint.task_id == task_id, TaskCheckpoint.status.in_(["APPROVED", "RESUMED"])))
            if checkpoint is not None and self.gateway is not None:
                task = db.get(Task, task_id)
                if task is not None:
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
                        return {"task_id": str(task_id), "status": "FAILED"}
                    existing = db.query(SkillExecutionLog).filter(
                        SkillExecutionLog.task_id == task.id,
                        SkillExecutionLog.tool_version_id == tool_id,
                        SkillExecutionLog.status == "SUCCEEDED",
                    ).first()
                    if existing is not None:
                        task.result = existing.result or {"data": None}
                        transition_task(db, task, TaskStatus.SUCCESS, "TASK_SKILL_IDEMPOTENT", "skill execution already completed")
                        db.commit()
                        return {"task_id": str(task_id), "status": "SUCCEEDED", "idempotent": True}
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
                        return {"task_id": str(task_id), "status": result.status}
                    task.result = {"skill_result": result.data}
                    transition_task(db, task, TaskStatus.SUCCESS, "TASK_SKILL_SUCCEEDED", "skill execution completed")
                    db.commit()
                    return {"task_id": str(task_id), "status": result.status}
            if self.model_gateway is not None:
                result = await self._execute_model_task(db, task_id)
                if result is not None:
                    return result
            execute_simulated(db, task_id)
            self._cancellation_events.pop(task_id, None)
        return {"task_id": str(task_id)}

    def request_cancellation(self, task_id: UUID) -> None:
        self._cancellation_events.setdefault(task_id, asyncio.Event()).set()

    async def _execute_model_task(self, db, task_id: UUID) -> dict | None:
        task = db.get(Task, task_id)
        if task is None or task.conversation_id is None:
            return None
        if task.status != TaskStatus.QUEUED:
            return {"task_id": str(task_id), "status": str(task.status)}
        claimed = db.execute(
            update(Task).where(Task.id == task_id, Task.status == TaskStatus.QUEUED)
            .values(status=TaskStatus.RUNNING, attempt_count=Task.attempt_count + 1)
        )
        if claimed.rowcount != 1:
            return {"task_id": str(task_id), "status": "SKIPPED"}
        db.commit()
        cancel_event = self._cancellation_events.setdefault(task_id, asyncio.Event())
        task = db.get(Task, task_id)
        if task is None:
            return None
        conversation = db.get(Conversation, task.conversation_id)
        trigger = db.get(Message, task.trigger_message_id) if task.trigger_message_id else None
        if conversation is None or trigger is None:
            return self._fail_model_task(db, task, "AI_TASK_CONTEXT_INVALID", "task context is invalid")
        agent_version = db.get(AgentVersion, task.agent_version_id)
        messages = build_context(db, conversation, system_prompt=(agent_version.config_snapshot or {}).get("system_prompt") if agent_version else None)
        mappings, tools = self._build_tool_mappings(db, agent_version, task.tenant_id)
        request = ModelRequest(
            trace_id=str(task.id), task_id=task.id, conversation_id=conversation.id,
            agent_version_id=task.agent_version_id, model=get_settings().model_name,
            messages=messages, tools=tools, timeout_seconds=get_settings().model_timeout_seconds,
        )
        try:
            if mappings and self.gateway is not None:
                response = (await AgentExecutionLoop(self.model_gateway, self.gateway).run(
                    request=request,
                    mappings=mappings,
                    db=db,
                    tenant_id=task.tenant_id,
                    user_id=task.owner_user_id,
                    task_id=task.id,
                    cancellation_event=cancel_event,
                    deadline_at=task.deadline_at,
                )).response
            else:
                response = await self.model_gateway.generate(request)
        except ModelGatewayError as exc:
            return self._fail_model_task(db, task, exc.code, exc.message)
        if response.tool_calls:
            return self._fail_model_task(db, task, "AI_TOOL_MAPPING_INVALID", "tool execution requires an agent tool mapping")
        if not response.content:
            return self._fail_model_task(db, task, "AI_MODEL_RESPONSE_INVALID", "model returned an empty response")
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
        result_message = Message(tenant_id=task.tenant_id, conversation_id=conversation.id, sender_type="ASSISTANT", content=response.content, status="COMPLETED", task_id=task.id)
        db.add(result_message)
        db.flush()
        task.result_message_id = result_message.id
        task.result = {"content": response.content, "usage": response.usage.__dict__ if response.usage else None}
        transition_task(db, task, TaskStatus.SUCCESS, "TASK_MODEL_SUCCEEDED", "model task completed")
        db.commit()
        return {"task_id": str(task_id), "status": "SUCCEEDED"}

    @staticmethod
    def _build_tool_mappings(db, agent_version: AgentVersion | None, tenant_id: UUID) -> tuple[dict[str, ToolMapping], list[dict]]:
        if agent_version is None:
            return {}, []
        mappings: dict[str, ToolMapping] = {}
        tools: list[dict] = []
        links = db.query(AgentVersionSkill).filter(
            AgentVersionSkill.agent_version_id == agent_version.id,
            AgentVersionSkill.tenant_id == tenant_id,
        ).all()
        for agent_skill in links:
            skill = db.get(SkillVersion, agent_skill.skill_version_id)
            if skill is None or skill.tenant_id != tenant_id or skill.status != "PUBLISHED":
                continue
            # 父级 Skill 已归档时其版本不可再被映射为工具
            parent_skill = db.get(Skill, skill.skill_id)
            if parent_skill is None or parent_skill.status != "ACTIVE":
                continue
            tool_links = db.query(SkillVersionTool).filter(
                SkillVersionTool.skill_version_id == skill.id,
                SkillVersionTool.tenant_id == tenant_id,
            ).all()
            for tool_link in tool_links:
                tool_version = db.get(ToolVersion, tool_link.tool_version_id)
                tool = db.get(Tool, tool_version.tool_id) if tool_version else None
                if tool_version is None or tool is None or tool_version.tenant_id != tenant_id or tool.status != "ACTIVE" or tool_version.status != "PUBLISHED":
                    continue
                name = tool.name
                if name in mappings:
                    continue
                schema = tool_version.input_schema or {"type": "object", "properties": {}}
                alias = safe_function_name(name)
                mappings[alias] = ToolMapping(alias, skill.id, tool_version.id, schema)
                # 工具描述只取工具自身 description，禁止回退到 skill.instructions——
                # 流程指令属于 system prompt，塞进单个工具描述会误导模型把该函数当作流程中的其他工具
                desc = f"{name}：{(tool.description or '').strip()}".rstrip("：")[:512]
                tools.append({"type": "function", "function": {"name": alias, "description": desc, "parameters": schema}})
        # 委派：delegation_enabled 时把允许的子智能体发布版暴露为委派工具（仅一层，深度由配置约束）
        if agent_version.delegation_enabled:
            for child_id in agent_version.allowed_child_agent_versions or []:
                try:
                    child_ver_id = UUID(str(child_id))
                except (ValueError, AttributeError):
                    continue
                child = db.get(AgentVersion, child_ver_id)
                if child is None or child.tenant_id != tenant_id or child.status != "PUBLISHED":
                    continue
                child_agent = db.get(Agent, child.agent_id)
                if child_agent is None or child_agent.status != "ACTIVE":
                    continue
                alias = f"delegate_{hashlib.md5(str(child_ver_id).encode('utf-8')).hexdigest()[:12]}"
                if alias in mappings:
                    continue
                schema = {"type": "object", "properties": {"task": {"type": "string", "description": f"要委派给「{child_agent.name}」处理的完整任务描述，需包含该智能体所需的全部上下文"}}, "required": ["task"]}
                mappings[alias] = ToolMapping(alias, child_ver_id, None, schema, child_agent_version_id=child_ver_id)
                desc = f"委派给子智能体「{child_agent.name}」独立处理任务：{(child_agent.description or '').strip()}".rstrip("：")[:512]
                tools.append({"type": "function", "function": {"name": alias, "description": desc, "parameters": schema}})
        return mappings, tools

    @staticmethod
    def _fail_model_task(db, task: Task, code: str, message: str) -> dict:
        task.error_code = code
        task.error_message = message
        transition_task(db, task, TaskStatus.FAILED, "TASK_MODEL_FAILED", "model task failed")
        db.commit()
        return {"task_id": str(task.id), "status": "FAILED", "error_code": code}

    async def recover(self) -> None:
        with SessionLocal() as db:
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
            with SessionLocal() as db:
                found = await self.dispatcher.dispatch_once(db)
            if not found:
                await asyncio.sleep(get_settings().task_outbox_poll_interval)

    async def _timeout_loop(self) -> None:
        while True:
            with SessionLocal() as db:
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
