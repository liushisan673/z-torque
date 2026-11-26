from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.servertime import UTCDatetime


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)


class KnowledgeBaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    status: str
    created_at: UTCDatetime
    updated_at: UTCDatetime


class KnowledgeBaseDocumentAttach(BaseModel):
    file_id: UUID


class KnowledgeBaseDocumentResponse(BaseModel):
    file_id: UUID
    file_name: str
    media_type: str
    size_bytes: int
    file_status: str
    indexed: bool
    attached_at: UTCDatetime


class AgentKnowledgeBaseBindingRequest(BaseModel):
    knowledge_base_ids: list[UUID] = Field(default_factory=list, max_length=50)
