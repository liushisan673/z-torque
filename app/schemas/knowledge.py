from pydantic import BaseModel, Field


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = 5


class CitationVerifyRequest(BaseModel):
    answer: str
    chunk_ids: list[str] = Field(default_factory=list)
