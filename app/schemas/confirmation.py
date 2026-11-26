from uuid import UUID

from pydantic import BaseModel


class ConfirmationResolveRequest(BaseModel):
    approved: bool


class ConfirmationResponse(BaseModel):
    id: UUID
    status: str
    requested_by_user_id: UUID
    confirmed_by_user_id: UUID | None
    reason: str | None
    task_id: UUID | None
    skill_version_id: UUID | None
    tool_version_id: UUID | None
    resolved_at: object | None
    # 展示用（非必填）：管理员可见同租户他人确认单，仅凭 UUID 无法辨认发起人
    requested_by_username: str | None = None
    # 仅返回可安全展示的执行摘要，不暴露原始参数、脚本正文或沙箱绝对路径。
    request_summary: dict | None = None
    can_resolve: bool = False

    model_config = {"from_attributes": True}
