"""MCP 工具漂移比对：只报告差异，不自动改写本地登记（改动需人工确认后走 /tools/sync）。"""

from types import SimpleNamespace
from uuid import uuid4

from app.api.routes.connectors import (
    _diff_mcp_tools,
    _normalize_discovered_tools,
    _RegisteredMcpTool,
)


def _registered(name: str, description: str = "", schema: dict | None = None) -> _RegisteredMcpTool:
    return _RegisteredMcpTool(
        name=name,
        tool_id=uuid4(),
        tool_name=name,
        description=description,
        input_schema=schema or {},
        version=SimpleNamespace(),
    )


def test_reports_added_removed_changed_and_unchanged():
    registered = {
        "keep": _registered("keep", "same", {"type": "object", "properties": {}}),
        "desc": _registered("desc", "old", {"type": "object", "properties": {}}),
        "schema": _registered("schema", "same", {"type": "object", "properties": {"a": {"type": "string"}}}),
        "gone": _registered("gone", "x"),
    }
    discovered = [
        {"name": "keep", "description": "same", "input_schema": {"type": "object", "properties": {}}},
        {"name": "desc", "description": "new", "input_schema": {"type": "object", "properties": {}}},
        {
            "name": "schema",
            "description": "same",
            "input_schema": {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]},
        },
        {"name": "fresh", "description": "brand new", "input_schema": {}},
    ]

    drift = _diff_mcp_tools(discovered, registered)

    assert [item["name"] for item in drift["added"]] == ["fresh"]
    assert [item["name"] for item in drift["removed"]] == ["gone"]
    assert {item["name"]: item["fields"] for item in drift["changed"]} == {
        "desc": ["description"],
        "schema": ["input_schema"],
    }
    assert drift["unchanged"] == 1
    assert drift["has_drift"] is True
    # 变更项要带上本地/远端说明，供前端直接展示
    desc_change = next(item for item in drift["changed"] if item["name"] == "desc")
    assert desc_change["local_description"] == "old"
    assert desc_change["remote_description"] == "new"


def test_schema_key_order_is_not_a_change():
    registered = {
        "t": _registered(
            "t",
            "d",
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
        )
    }
    discovered = [
        {
            "name": "t",
            "description": "d",
            "input_schema": {"required": ["a"], "properties": {"a": {"type": "string"}}, "type": "object"},
        }
    ]

    drift = _diff_mcp_tools(discovered, registered)

    assert drift["has_drift"] is False
    assert drift["unchanged"] == 1


def test_no_drift_when_remote_matches_local():
    registered = {"t": _registered("t", "d", {"type": "object"})}

    drift = _diff_mcp_tools([{"name": "t", "description": "d", "input_schema": {"type": "object"}}], registered)

    assert drift == {"added": [], "removed": [], "changed": [], "unchanged": 1, "has_drift": False}


def test_normalize_converts_mcp_camelcase_schema_and_drops_nameless_tools():
    listing = {
        "tools": [
            {"name": "a", "description": None, "inputSchema": {"type": "object"}},
            {"description": "no name"},
        ]
    }

    assert _normalize_discovered_tools(listing) == [
        {"name": "a", "description": "", "input_schema": {"type": "object"}}
    ]
