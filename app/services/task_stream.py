import asyncio
import json
import time
from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kernel.task.states import TERMINAL_STATES
from app.models import Task, TaskEvent

_POLL_INTERVAL_SECONDS = 1.0
# 单次连接最长挂起时长：到时发 task.done 断开，前端可用 Last-Event-ID 重连续传
_MAX_STREAM_SECONDS = 300.0


def task_event_stream(db: Session, *, task_id: UUID, tenant_id: UUID, last_event_id: str | None = None) -> AsyncIterator[str]:
    async def generate():
        task = db.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id))
        if task is None:
            return
        try:
            last_seq = int(last_event_id or 0)
        except ValueError:
            last_seq = 0
        snapshot = {
            "task_id": str(task.id),
            "status": str(task.status),
            "result": task.result,
            "root_task_id": str(task.root_task_id),
            "parent_task_id": None if task.parent_task_id is None else str(task.parent_task_id),
            "delegation_depth": int(task.delegation_depth),
            "child_sequence": task.child_sequence,
        }
        yield f"id: snapshot\nevent: task.snapshot\ndata: {json.dumps(snapshot, default=str)}\n\n"
        # 回放历史事件（Last-Event-ID 续传：只推 seq 更大者）
        events = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.seq > last_seq).order_by(TaskEvent.seq)).all()
        for event in events:
            yield _format_event(event, task)
            last_seq = event.seq
        if task.status in TERMINAL_STATES:
            yield _format_done(task)
            return
        # 实时推送：轮询新事件，任务终态且无新事件后结束
        started = time.monotonic()
        while True:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            db.expire_all()
            task = db.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id))
            if task is None:
                return
            events = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.seq > last_seq).order_by(TaskEvent.seq)).all()
            for event in events:
                yield _format_event(event, task)
                last_seq = event.seq
            if task.status in TERMINAL_STATES:
                yield _format_done(task)
                return
            if time.monotonic() - started > _MAX_STREAM_SECONDS:
                yield _format_done(task)
                return
    return generate()


def _format_event(event: TaskEvent, task: Task) -> str:
    payload = dict(event.payload_summary or {})
    payload.update({
        "task_id": str(task.id),
        "seq": event.seq,
        "summary": event.summary,
        "status": str(task.status),
        "root_task_id": str(task.root_task_id),
        "parent_task_id": None if task.parent_task_id is None else str(task.parent_task_id),
        "delegation_depth": int(task.delegation_depth),
        "child_sequence": task.child_sequence,
    })
    return f"id: {event.seq}\nevent: {event.event_type}\ndata: {json.dumps(payload, default=str)}\n\n"


def _format_done(task: Task) -> str:
    payload = {
        "task_id": str(task.id),
        "status": str(task.status),
        "result": task.result,
        "error_code": task.error_code,
        "error_message": task.error_message,
    }
    return f"event: task.done\ndata: {json.dumps(payload, default=str)}\n\n"
