from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response, trace_id_from
from app.db.session import get_db
from app.models import User
from app.services.memory_consolidation import MemoryConsolidationService

router = APIRouter(prefix="/memory", tags=["memory"])


class MemoryConsolidateRequest(BaseModel):
    scope: Literal["USER", "AGENT", "CONVERSATION"]
    agent_id: UUID | None = Field(default=None)


@router.post("/consolidate", summary="Dream 式长期记忆整合")
async def consolidate_memory(request: Request, payload: MemoryConsolidateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    registry = getattr(request.app.state, "port_registry", None)
    store = registry.try_get("memory.long") if registry is not None else None
    if store is None:
        raise ApiError("AI_CAPABILITY_UNAVAILABLE", "memory.long is unavailable", 503)
    service = MemoryConsolidationService(store)
    result = service.consolidate(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        scope=payload.scope,
        agent_id=payload.agent_id,
        trace_id=trace_id_from(request),
    )
    db.commit()
    return success_response(request, result)
