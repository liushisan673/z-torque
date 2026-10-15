from uuid import UUID
from pydantic import BaseModel


class AgentSkillAttachRequest(BaseModel):
    skill_version_id: UUID
