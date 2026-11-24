from dataclasses import dataclass
from uuid import UUID

from app.models import Message


@dataclass(frozen=True)
class ChatResult:
    user_message: Message
    assistant_message: Message | None
    task_id: UUID | None = None
    mocked: bool = False
    # 写入类工具触发人工审批时的挂起信息（confirmation_id/tool_name/arguments/task_id）
    pending_confirmation: dict | None = None
    # 无任务直答（无工具智能体）时的回复文本：随 POST /chat 响应直接返回，供前端流式渲染
    response_content: str | None = None
