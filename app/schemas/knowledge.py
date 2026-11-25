from uuid import UUID

from pydantic import BaseModel, Field


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)
    knowledge_base_id: UUID | None = None


class CitationVerifyRequest(BaseModel):
    answer: str
    chunk_ids: list[str] = Field(default_factory=list)
