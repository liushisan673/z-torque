"""P1b UX 返工任务 1：capabilities 结构化能力卡片真实入口验收。

负向先行：旧实现没有 capability_cards，也没有后端 reason/actions；
前端不得自行编造原因。本文件先锁定后端契约，再允许实现。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.capability_presentation import build_capability_cards

BARE_TERMS = ("AVAILABLE", "DEGRADED", "UNAVAILABLE", "MCP", "token", "unknown", "execution", "lifecycle")


def _card(payload: dict, key: str) -> dict:
    cards = payload["capability_cards"]
    assert key in cards
    card = cards[key]
    for field in ("state", "label", "reason_code", "reason", "actions"):
        assert field in card, f"{key} missing {field}"
    assert card["state"] in {"AVAILABLE", "DEGRADED", "UNAVAILABLE"}
    assert isinstance(card["label"], str) and card["label"]
    assert isinstance(card["reason"], str)
    assert isinstance(card["actions"], list)
    for action in card["actions"]:
        assert action["label"]
        assert action.get("route") or action.get("action_code")
    for term in BARE_TERMS:
        assert term not in card["reason"], f"bare term {term} leaked into reason"
    return card


def test_capability_cards_unavailable_reason_actions_from_real_state():
    cards = build_capability_cards(
        mcp={"execution": "UNAVAILABLE"},
        knowledge={"local": {"search": "UNAVAILABLE"}},
        model={"configured": False, "name": ""},
    )
    payload = {"capability_cards": cards}

    tools = _card(payload, "external_tools")
    assert tools["state"] == "UNAVAILABLE"
    assert tools["reason_code"] == "MCP_NOT_CONNECTED"
    assert tools["reason"] == "未连接外部工具服务"
    assert {"label": "去配置连接器", "route": "/connectors"} in tools["actions"]
    assert {"label": "重新测试连接", "action_code": "RETEST_MCP"} in tools["actions"]

    knowledge = _card(payload, "knowledge")
    assert knowledge["state"] == "UNAVAILABLE"
    assert knowledge["reason_code"] == "KNOWLEDGE_NOT_INITIALIZED"
    assert knowledge["reason"] == "知识库未初始化"
    assert {"label": "去上传资料", "action_code": "UPLOAD_KNOWLEDGE"} in knowledge["actions"]
    assert {"label": "重建索引", "action_code": "REINDEX_KNOWLEDGE"} in knowledge["actions"]

    model = _card(payload, "model")
    assert model["state"] == "UNAVAILABLE"
    assert model["reason_code"] == "MODEL_NOT_CONFIGURED"
    assert model["reason"] == "未配置模型"
    assert {"label": "去模型管理", "route": "/models"} in model["actions"]


def test_capability_cards_degraded_reasons_from_real_state():
    cards = build_capability_cards(
        mcp={"execution": "AVAILABLE", "lifecycle": "UNAVAILABLE"},
        knowledge={"local": {"search": "DEGRADED"}},
        model={"configured": True, "name": ""},
    )
    payload = {"capability_cards": cards}

    tools = _card(payload, "external_tools")
    assert tools["state"] == "DEGRADED"
    assert tools["reason_code"] == "MCP_LIFECYCLE_UNAVAILABLE"

    knowledge = _card(payload, "knowledge")
    assert knowledge["state"] == "DEGRADED"
    assert knowledge["reason_code"] == "KNOWLEDGE_INDEX_DEGRADED"
    assert knowledge["reason"] == "本地检索已降级，结果可能不完整"

    model = _card(payload, "model")
    assert model["state"] == "DEGRADED"
    assert model["reason_code"] == "MODEL_NAME_NOT_SELECTED"


def test_capability_cards_available_has_no_reason_but_keeps_management_action():
    cards = build_capability_cards(
        mcp={"execution": "AVAILABLE", "lifecycle": "AVAILABLE"},
        knowledge={"local": {"search": "AVAILABLE"}},
        model={"configured": True, "name": "deepseek-flash"},
    )
    payload = {"capability_cards": cards}
    for key in ("external_tools", "knowledge", "model"):
        card = _card(payload, key)
        assert card["state"] == "AVAILABLE"
        assert card["reason_code"] is None
        assert card["reason"] == ""
        assert card["actions"]


@pytest.mark.asyncio
async def test_capabilities_api_exposes_cards_and_keeps_legacy_fields():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/z-torque/capabilities")
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    for legacy in ("stage", "features", "ports", "tool_executors", "mcp", "knowledge", "model", "context", "usage", "memory"):
        assert legacy in data, f"legacy field {legacy} disappeared"

    cards = data["capability_cards"]
    assert cards["external_tools"]["state"] == data["mcp"]["execution"]
    assert cards["knowledge"]["state"] == data["knowledge"]["local"]["search"]
    _card(data, "external_tools")
    _card(data, "knowledge")
    _card(data, "model")


@pytest.mark.asyncio
async def test_capabilities_api_unavailable_card_derived_from_assembly(monkeypatch):
    """构造真实装配缺口：卡片必须随 capabilities 旧字段一起变为不可用。"""
    monkeypatch.setattr(app.state, "assembly_plugins", ())
    monkeypatch.setattr(app.state, "skill_gateway", None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/z-torque/capabilities")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["mcp"]["execution"] == "UNAVAILABLE"
    card = data["capability_cards"]["external_tools"]
    assert card["state"] == "UNAVAILABLE"
    assert card["reason_code"] == "MCP_NOT_CONNECTED"
    assert card["reason"] == "未连接外部工具服务"
