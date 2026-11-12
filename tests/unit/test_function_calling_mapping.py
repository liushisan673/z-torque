"""工具名映射：模型按系统提示输出原始工具名时也能解析到同一映射。"""

from uuid import uuid4

import pytest

from app.core.errors import ApiError
from app.kernel.model_gateway import ModelToolCall
from app.services.function_calling import ToolMapping, map_tool_calls


def _mapping() -> ToolMapping:
    return ToolMapping(
        "tool_abc123",
        uuid4(),
        None,
        {"type": "object", "properties": {}},
        original_name="weixin.send_text",
    )


def test_original_tool_name_resolves_to_alias():
    mapping = _mapping()
    calls = [ModelToolCall("call-1", "weixin.send_text", {})]
    result = map_tool_calls(calls, {"tool_abc123": mapping})
    assert result[0][0] is mapping


def test_alias_name_still_resolves():
    mapping = _mapping()
    calls = [ModelToolCall("call-1", "tool_abc123", {})]
    result = map_tool_calls(calls, {"tool_abc123": mapping})
    assert result[0][0] is mapping


def test_unknown_tool_name_rejected():
    calls = [ModelToolCall("call-1", "not_a_tool", {})]
    with pytest.raises(ApiError) as exc:
        map_tool_calls(calls, {"tool_abc123": _mapping()})
    assert exc.value.code == "AI_TOOL_MAPPING_INVALID"
