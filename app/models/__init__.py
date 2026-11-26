from app.db.base import Base
from app.models.agent import Agent, AgentVersion, AgentVersionSkill
from app.models.audit import AuditLog
from app.models.auth_session import AuthSession
from app.models.confirmation import Confirmation
from app.models.connector import Connector, ConnectorTool
from app.models.context_summary import ContextSummary
from app.models.conversation import Conversation
from app.models.file import File
from app.models.file_version_change import FileVersionChange
from app.models.identity import Permission, Role, Tenant, User, role_permission, user_role
from app.models.knowledge import KnowledgeChunk, KnowledgeDoc
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseDocument
from app.models.knowledge_vector import KnowledgeVector
from app.models.memory import ShortTermMemory
from app.models.memory_long import LongMemory, LongMemoryVersion
from app.models.message import Message, message_file
from app.models.model_resource import ModelEndpoint, ModelProvider, ModelResource
from app.models.patch_artifact import PatchArtifact
from app.models.task import Outbox, Task, TaskCheckpoint, TaskEvent
from app.models.tool_skill import (
    Skill,
    SkillExecutionLog,
    SkillVersion,
    SkillVersionTool,
    Tool,
    ToolVersion,
)
from app.models.usage_summary import UsageSummary as UsageSummary
from app.models.webhook import InboundWebhook, InboundWebhookEvent

# 插件自有表（AuditExportJob / AuditExportShard / AuditArchiveBatch）不在此处导出：
# 方案 b 下它们的模型定义跟随插件目录 `app/builtin_plugins/audit_export_archive/models.py`，
# 由 Alembic env 与测试 conftest 显式 import 以注册进同一个 Base.metadata。
__all__ = [
    "Agent",
    "AgentVersion",
    "AgentVersionSkill",
    "AuditLog",
    "AuthSession",
    "Base",
    "Confirmation",
    "Connector",
    "ConnectorTool",
    "ContextSummary",
    "Conversation",
    "File",
    "FileVersionChange",
    "InboundWebhook",
    "InboundWebhookEvent",
    "KnowledgeBase",
    "KnowledgeBaseDocument",
    "KnowledgeVector",
    "KnowledgeChunk",
    "KnowledgeDoc",
    "LongMemory",
    "LongMemoryVersion",
    "Message",
    # 关联表本身不对外使用，但必须在包级 import 一次才会注册进 Base.metadata
    "message_file",
    "ModelEndpoint",
    "ModelProvider",
    "ModelResource",
    "Outbox",
    "PatchArtifact",
    "Permission",
    "Role",
    "ShortTermMemory",
    "Skill",
    "SkillExecutionLog",
    "SkillVersion",
    "SkillVersionTool",
    "Task",
    "TaskCheckpoint",
    "TaskEvent",
    "Tenant",
    "Tool",
    "ToolVersion",
    "User",
    "role_permission",
    "user_role",
]
