"""能力卡片：把 capabilities 的技术状态翻译成用户能懂的状态、原因和动作。

不新增内核 Port：纯函数，输入是 ``/capabilities`` 已从真实装配/配置中取到的
``mcp`` / ``knowledge`` / ``model`` 三段，不允许写死「可用」，也不输出
MCP / AVAILABLE / token / unknown 等裸术语到面向用户的 reason。
"""

from __future__ import annotations

from typing import Any


def _action(label: str, *, route: str | None = None, action_code: str | None = None) -> dict[str, str]:
    action = {"label": label}
    if route:
        action["route"] = route
    if action_code:
        action["action_code"] = action_code
    return action


def _card(
    state: str,
    label: str,
    reason_code: str | None,
    reason: str,
    actions: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "state": state,
        "label": label,
        "reason_code": reason_code,
        "reason": reason,
        "actions": actions,
    }


def build_capability_cards(*, mcp: dict, knowledge: dict, model: dict) -> dict[str, dict[str, Any]]:
    """按真实状态推导三张能力卡片；旧字段保持原样，本函数只做增量输出。"""
    execution = mcp.get("execution")
    lifecycle = mcp.get("lifecycle")
    if execution == "AVAILABLE" and lifecycle == "UNAVAILABLE":
        tools = _card(
            "DEGRADED", "外部工具", "MCP_LIFECYCLE_UNAVAILABLE", "外部工具服务可用，但工具发现受限",
            [_action("去配置连接器", route="/connectors"), _action("重新测试连接", action_code="RETEST_MCP")],
        )
    elif execution == "AVAILABLE":
        tools = _card(
            "AVAILABLE", "外部工具", None, "",
            [_action("去配置连接器", route="/connectors")],
        )
    else:
        tools = _card(
            "UNAVAILABLE", "外部工具", "MCP_NOT_CONNECTED", "未连接外部工具服务",
            [_action("去配置连接器", route="/connectors"), _action("重新测试连接", action_code="RETEST_MCP")],
        )

    search = (knowledge.get("local") or {}).get("search")
    if search == "AVAILABLE":
        local = _card(
            "AVAILABLE", "知识检索", None, "",
            [_action("去上传资料", action_code="UPLOAD_KNOWLEDGE")],
        )
    elif search == "DEGRADED":
        local = _card(
            "DEGRADED", "知识检索", "KNOWLEDGE_INDEX_DEGRADED", "本地检索已降级，结果可能不完整",
            [_action("去上传资料", action_code="UPLOAD_KNOWLEDGE"), _action("重建索引", action_code="REINDEX_KNOWLEDGE")],
        )
    else:
        local = _card(
            "UNAVAILABLE", "知识检索", "KNOWLEDGE_NOT_INITIALIZED", "知识库未初始化",
            [_action("去上传资料", action_code="UPLOAD_KNOWLEDGE"), _action("重建索引", action_code="REINDEX_KNOWLEDGE")],
        )

    configured = model.get("configured") is True
    model_name = str(model.get("name") or "").strip()
    if not configured:
        selected = _card(
            "UNAVAILABLE", "模型", "MODEL_NOT_CONFIGURED", "未配置模型",
            [_action("去模型管理", route="/models")],
        )
    elif not model_name:
        selected = _card(
            "DEGRADED", "模型", "MODEL_NAME_NOT_SELECTED", "已配置模型连接，但还没有选择默认模型",
            [_action("去模型管理", route="/models")],
        )
    else:
        selected = _card(
            "AVAILABLE", "模型", None, "",
            [_action("去模型管理", route="/models")],
        )

    return {"external_tools": tools, "knowledge": local, "model": selected}
