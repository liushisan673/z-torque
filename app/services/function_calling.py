import hashlib
import json
import re
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, ValidationError, validate

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
    # 非空表示这是站内 A2A 派发工具：执行时把任务交给该智能体版本，复用子执行链路
    dispatch_agent_version_id: UUID | None = None
    # 工具原始名；模型可能按系统提示直接输出原名而非 ASCII 别名
    original_name: str | None = None


def map_tool_calls(calls: list[ModelToolCall], mappings: dict[str, ToolMapping]) -> list[tuple[ToolMapping, dict[str, Any]]]:
    result = []
    for call in calls:
        mapping = mappings.get(call.name)
        if mapping is None and call.name:
            mapping = next((item for item in mappings.values() if item.original_name == call.name), None)
        if mapping is None:
            raise ApiError('AI_TOOL_MAPPING_INVALID', 'tool name is not allowed for this agent version', 422)
        try:
            validate(call.arguments, mapping.input_schema)
        except ValidationError as exc:
            raise ApiError('AI_FUNCTION_CALL_INVALID', 'tool arguments do not match the schema', 422, {'path': list(exc.path)}) from exc
        result.append((mapping, call.arguments))
    return result


@dataclass(frozen=True)
class ToolCallValidation:
    call: ModelToolCall
    mapping: ToolMapping | None
    arguments: dict[str, Any]
    error: dict[str, Any] | None = None


def _json_path(path: Any) -> str:
    parts = [str(part) for part in list(path)] if path else []
    return "$" + "".join(f".{part}" if str(part).isidentifier() else f"[{part}]" for part in parts)


def _did_you_mean(error: ValidationError, schema: dict[str, Any]) -> str | None:
    instance = error.instance
    validator = error.validator
    if validator == "enum":
        choices = [str(item) for item in error.validator_value or []]
        match = get_close_matches(str(instance), choices, n=1, cutoff=0.4)
        return match[0] if match else None
    if validator == "required":
        candidates = list((error.validator_value or [])) + list((schema.get("properties") or {}).keys())
        missing = str(error.message).split("'")[1] if "'" in error.message else str(instance)
        match = get_close_matches(missing, [str(item) for item in candidates], n=1, cutoff=0.4)
        return match[0] if match else None
    if validator == "type" and isinstance(instance, str):
        expected = error.validator_value
        try:
            if expected == "integer" and str(int(instance)) == instance.strip():
                return str(int(instance))
            if expected == "number" and str(float(instance)) == instance.strip():
                return str(float(instance))
        except (TypeError, ValueError):
            return None
    return None


def validate_tool_calls(calls: list[ModelToolCall], mappings: dict[str, ToolMapping]) -> list[ToolCallValidation]:
    """P1a-5：Draft 2020-12 严格校验，返回结构化错误而不是提前抛异常。"""
    validated: list[ToolCallValidation] = []
    for call in calls:
        mapping = mappings.get(call.name)
        if mapping is None and call.name:
            mapping = next((item for item in mappings.values() if item.original_name == call.name), None)
        if mapping is None:
            validated.append(ToolCallValidation(call, None, {}, {"code": "AI_TOOL_MAPPING_INVALID", "json_path": "$", "message": "tool name is not allowed for this agent version"}))
            continue
        schema = mapping.input_schema or {"type": "object"}
        try:
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(call.arguments)
        except ValidationError as exc:
            hint = _did_you_mean(exc, schema)
            validated.append(ToolCallValidation(call, mapping, {}, {
                "code": "AI_FUNCTION_CALL_INVALID",
                "json_path": _json_path(exc.absolute_path),
                "message": exc.message[:300],
                "did_you_mean": hint,
                "self_correction": (
                    "请只修正该工具调用的参数后重发，保持 tool_call_id 不变；"
                    f"错误位置 {_json_path(exc.absolute_path)}：{exc.message[:200]}"
                    + (f"；你可能想填 {hint}" if hint else "")
                    + "。这是唯一一次结构化修复回合。"
                ),
            }))
            continue
        except Exception as exc:  # noqa: BLE001 - schema 本身非法必须显式失败
            validated.append(ToolCallValidation(call, mapping, {}, {"code": "AI_FUNCTION_CALL_INVALID", "json_path": "$", "message": f"invalid tool schema: {exc}"[:300]}))
            continue
        validated.append(ToolCallValidation(call, mapping, call.arguments, None))
    return validated


def repair_tool_message(call: ModelToolCall, error: dict[str, Any]) -> str:
    """回喂模型的单条结构化错误消息，不含用户正文或密钥。"""
    payload = {
        "status": "INVALID_ARGUMENTS",
        "tool_name": call.name,
        "tool_call_id": call.id,
        "error_code": error["code"],
        "json_path": error["json_path"],
        "message": error["message"],
    }
    if error.get("did_you_mean") is not None:
        payload["did_you_mean"] = error["did_you_mean"]
    payload["self_correction"] = error["self_correction"]
    return json.dumps(payload, ensure_ascii=False)
