from uuid import UUID

from pydantic import BaseModel, Field


class PatchOpRequest(BaseModel):
    path: str
    old_text: str
    new_text: str
    expected_hash: str


class PatchApplyRequest(BaseModel):
    base_dir: str
    patches: list[PatchOpRequest] = Field(min_length=1)
    task_id: UUID | None = None
