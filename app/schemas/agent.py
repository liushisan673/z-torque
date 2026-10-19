from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field

from app.core.servertime import UTCDatetime

class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None

class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None

class VersionCreate(BaseModel):
    config_snapshot: dict = Field(default_factory=dict)
    # 委派配置存于 AgentVersion 独立列；PATCH 按 exclude_unset 语义处理（未提供=不修改）
    delegation_enabled: bool | None = None
    allowed_child_agent_versions: list[str] | None = None
    child_necessity_policy: dict | None = None

class AgentVersionArchiveCheck(BaseModel):
    """版本归档预检结果（GET /agents/{id}/versions/{vid}/archive-check）。"""
    can_archive: bool
    already_archived: bool = False
    reasons: list[dict] = Field(default_factory=list)
    warnings: list[dict] = Field(default_factory=list)
    running_task_count: int = 0
    referenced_by: list[dict] = Field(default_factory=list)

class AgentResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    source_type: str
    source_agent_id: UUID | None
    source_agent_version_id: UUID | None
    current_published_version_id: UUID | None
    status: str
    is_default_parent: bool
    owner_user_id: UUID | None = None
    created_at: UTCDatetime
    model_config = {"from_attributes": True}

class AgentVersionResponse(BaseModel):
    id: UUID
    agent_id: UUID
    version_number: int
    status: str
    config_snapshot: dict
    snapshot_hash: str | None
    delegation_enabled: bool
    allowed_child_agent_versions: list[str] | None
    child_necessity_policy: dict | None
    delegation_capability_version: str | None
    published_at: UTCDatetime | None
    model_config = {"from_attributes": True}