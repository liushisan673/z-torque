from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response
from app.db.session import get_db
from app.kernel.task.service import create_task, transition_task
from app.kernel.task.states import TaskStatus
from app.models import Task, TaskEvent, User
from app.schemas.task import TaskCreateRequest, TaskEventResponse, TaskResponse
from app.services.task_stream import task_event_stream

router = APIRouter(prefix="/tasks", tags=["tasks"])
_TASK_NOT_FOUND = "task not found"


@router.post("", response_model=None)
async def create(payload: TaskCreateRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = create_task(db, tenant_id=user.tenant_id, owner_user_id=user.id, trace_id=request.state.trace_id, **payload.model_dump())
    db.commit()
    db.refresh(task)
    return success_response(request, TaskResponse.model_validate(task).model_dump(mode="json"))


@router.get("")
async def list_tasks(request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), agent_id: UUID | None = Query(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = select(Task).where(Task.owner_user_id == user.id)
    if agent_id is not None:
        query = query.where(Task.agent_id == agent_id)
    tasks = db.scalars(query.order_by(Task.created_at.desc()).offset(offset).limit(limit)).all()
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    return success_response(request, {"items": [TaskResponse.model_validate(task).model_dump(mode="json") for task in tasks], "offset": offset, "limit": limit, "total": total})


@router.get("/{task_id}")
async def get_task(task_id: str, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = db.scalar(select(Task).where(Task.id == task_id, Task.owner_user_id == user.id))
    if task is None:
        raise ApiError("AI_TASK_NOT_FOUND", _TASK_NOT_FOUND, 404)
    return success_response(request, TaskResponse.model_validate(task).model_dump(mode="json"))


@router.get("/{task_id}/events")
async def events(task_id: str, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = db.scalar(select(Task).where(Task.id == task_id, Task.owner_user_id == user.id))
    if task is None:
        raise ApiError("AI_TASK_NOT_FOUND", _TASK_NOT_FOUND, 404)
    rows = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.seq)).all()
    return success_response(request, {"items": [TaskEventResponse.model_validate(row).model_dump(mode="json") for row in rows]})


@router.get("/{task_id}/stream")
async def stream(task_id: str, request: Request, last_event_id: str | None = Header(None, alias="Last-Event-ID"), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = db.scalar(select(Task).where(Task.id == task_id, Task.owner_user_id == user.id, Task.tenant_id == user.tenant_id))
    if task is None:
        raise ApiError("AI_TASK_NOT_FOUND", _TASK_NOT_FOUND, 404)
    return StreamingResponse(task_event_stream(db, task_id=task.id, tenant_id=user.tenant_id, last_event_id=last_event_id), media_type="text/event-stream")


@router.post("/{task_id}/cancel")
async def cancel(task_id: str, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = db.scalar(select(Task).where(Task.id == task_id, Task.owner_user_id == user.id))
    if task is None:
        raise ApiError("AI_TASK_NOT_FOUND", _TASK_NOT_FOUND, 404)
    if task.status in {TaskStatus.PENDING, TaskStatus.QUEUED}:
        transition_task(db, task, TaskStatus.CANCELLED, "TASK_CANCELLED", "task cancelled", request.state.trace_id)
    elif task.status == TaskStatus.RUNNING:
        task.cancel_requested_at = datetime.now(timezone.utc)
        transition_task(db, task, TaskStatus.CANCELING, "TASK_CANCEL_REQUESTED", "task cancellation requested", request.state.trace_id)
        runtime = getattr(request.app.state, "task_runtime", None)
        if runtime is not None:
            runtime.request_cancellation(task.id)
    elif task.status in {TaskStatus.CANCELING, TaskStatus.CANCELLED}:
        return success_response(request, TaskResponse.model_validate(task).model_dump(mode="json"))
    elif task.status in {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.TIMEOUT}:
        raise ApiError("AI_TASK_NOT_CANCELABLE", "task cannot be cancelled", 409)
    else:
        raise ApiError("AI_TASK_NOT_CANCELABLE", "task cannot be cancelled", 409)
    db.commit()
    return success_response(request, TaskResponse.model_validate(task).model_dump(mode="json"))
