from uuid import UUID

from pydantic import BaseModel, Field

from app.core.servertime import UTCDatetime


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    # 一步创建并发布（M12-2）：publish=true 时创建智能体 + 首版本 + 绑定技能 + 发布一次完成
    publish: bool = False
    config_snapshot: dict | None = None
    skill_version_ids: list[UUID] | None = None

class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None

class VersionCreate(BaseModel):
    config_snapshot: dict = Field(default_factory=dict)
    # Deprecated compatibility fields: accepted from older clients but ignored by the service.
    delegation_enabled: bool | None = Field(default=None, deprecated=True, description="已废弃：Agent 委派统一使用 A2A Discovery")
    allowed_child_agent_versions: list[str] | None = Field(default=None, deprecated=True, description="已废弃：不再配置静态父子 Agent 白名单")
    child_necessity_policy: dict | None = Field(default=None, deprecated=True, description="已废弃：委派限制由统一调度策略管理")

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
    is_default: bool
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