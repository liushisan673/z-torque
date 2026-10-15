import json
from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Task, TaskEvent


def task_event_stream(db: Session, *, task_id: UUID, tenant_id: UUID, last_event_id: str | None = None) -> AsyncIterator[str]:
    async def generate():
        task = db.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id))
        if task is None:
            return
        try:
            start = int(last_event_id or 0)
        except ValueError:
            start = 0
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
        events = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.seq > start).order_by(TaskEvent.seq)).all()
        for event in events:
            payload = event.payload_summary or {}
            payload.update({
                "task_id": str(task_id),
                "seq": event.seq,
                "summary": event.summary,
                "root_task_id": str(task.root_task_id),
                "parent_task_id": None if task.parent_task_id is None else str(task.parent_task_id),
                "delegation_depth": int(task.delegation_depth),
                "child_sequence": task.child_sequence,
            })
            yield f"id: {event.seq}\nevent: {event.event_type}\ndata: {json.dumps(payload, default=str)}\n\n"
    return generate()
