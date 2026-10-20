"""短记忆空闲阈值判定（触发策略）。

摘要生成/读取能力已按 memory.short Port 收口（app/builtin_plugins/short_memory.py），
本模块仅保留触发策略判定，供会话列表接口按需触发摘要。
"""

from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.models import Conversation


def should_summarize_for_idle(conversation: Conversation) -> bool:
    if conversation.last_message_at is None:
        return False
    timestamp = conversation.last_message_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - timestamp >= timedelta(minutes=get_settings().memory_short_idle_minutes)
