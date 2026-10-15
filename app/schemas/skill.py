from uuid import UUID
from pydantic import BaseModel, Field


class ToolCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    tool_type: str = Field(min_length=1, max_length=32)


class ToolVersionCreateRequest(BaseModel):
    config_snapshot: dict = Field(default_factory=dict)
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict | None = None
    required_permissions: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_retries: int = Field(default=0, ge=0, le=3)
    retry_backoff_seconds: float = Field(default=0.5, ge=0, le=30)


class SkillCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    skill_type: str = "STANDARD"


class SkillUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None


class SkillVersionCreateRequest(BaseModel):
    instructions: str | None = None
    script_source: str | None = None
    required_permissions: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False


class SkillVersionUpdateRequest(BaseModel):
    instructions: str | None = None
    script_source: str | None = None
    required_permissions: list[str] | None = None
    requires_confirmation: bool | None = None


class AttachToolRequest(BaseModel):
    tool_version_id: UUID
