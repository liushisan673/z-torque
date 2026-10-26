import hashlib
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from jsonschema import ValidationError, validate

from app.core.errors import ApiError
from app.kernel.model_gateway import ModelToolCall

_FUNCTION_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


def safe_function_name(name: str) -> str:
    """模型函数名仅允许 ASCII 字母数字下划线连字符；中文名等不合规名称派生确定性别名。"""
    if _FUNCTION_NAME_PATTERN.fullmatch(name):
        return name
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:12]
    return f"tool_{digest}"


@dataclass(frozen=True)
class ToolMapping:
    model_name: str
    skill_version_id: UUID
    tool_version_id: UUID | None
    input_schema: dict[str, Any]
    # 非空表示这是委派工具：skill_version_id 字段复用为子智能体版本 ID，执行时走子智能体而非 skill
    child_agent_version_id: UUID | None = None


def map_tool_calls(calls: list[ModelToolCall], mappings: dict[str, ToolMapping]) -> list[tuple[ToolMapping, dict[str, Any]]]:
    result = []
    for call in calls:
        mapping = mappings.get(call.name)
        if mapping is None:
            raise ApiError('AI_TOOL_MAPPING_INVALID', 'tool name is not allowed for this agent version', 422)
        try:
            validate(call.arguments, mapping.input_schema)
        except ValidationError as exc:
            raise ApiError('AI_FUNCTION_CALL_INVALID', 'tool arguments do not match the schema', 422, {'path': list(exc.path)}) from exc
        result.append((mapping, call.arguments))
    return result
