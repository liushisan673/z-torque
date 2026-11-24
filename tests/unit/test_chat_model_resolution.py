"""对话模型解析：删除 mock 兜底后的显式契约。

决策（阶段13）：新部署没有模型时必须**显式报错**并要求先配置，
而不是静默降级为 MockModelGateway 返回假回复。Mock 仅由测试注入。
"""

from __future__ import annotations

import pytest

from app.core.errors import ApiError
from app.services.chat_application import resolve_chat_model_name


class _ConfiguredGateway:
    """模拟部署方显式配置了全局模型（env MODEL_BASE_URL/API_KEY/NAME 齐全）。"""

    default_model = "deepseek-flash"


class _UnconfiguredGateway:
    """模拟全新部署：装配阶段 default_gateway 为 None → 无默认模型。"""

    default_model = ""


def test_snapshot_binding_wins():
    name = resolve_chat_model_name(snapshot_model="deepseek-reasoner", gateway=_ConfiguredGateway())
    assert name == "deepseek-reasoner"


def test_snapshot_binding_wins_over_global_default():
    """快照语义：发布时冻结的绑定不随全局配置漂移。"""
    name = resolve_chat_model_name(snapshot_model="qwen-test", gateway=_ConfiguredGateway())
    assert name == "qwen-test"


def test_falls_back_to_explicitly_configured_global_model():
    name = resolve_chat_model_name(snapshot_model=None, gateway=_ConfiguredGateway())
    assert name == "deepseek-flash"


def test_blank_snapshot_is_treated_as_absent():
    name = resolve_chat_model_name(snapshot_model="   ", gateway=_ConfiguredGateway())
    assert name == "deepseek-flash"


def test_unconfigured_gateway_raises_model_not_configured():
    with pytest.raises(ApiError) as excinfo:
        resolve_chat_model_name(snapshot_model=None, gateway=_UnconfiguredGateway())
    assert excinfo.value.code == "AI_MODEL_NOT_CONFIGURED"
    assert excinfo.value.status_code == 409


def test_missing_gateway_raises_model_not_configured():
    """网关整体缺失（未装配）同样必须报错，不能假设有 mock。"""
    with pytest.raises(ApiError) as excinfo:
        resolve_chat_model_name(snapshot_model=None, gateway=None)
    assert excinfo.value.code == "AI_MODEL_NOT_CONFIGURED"


def test_mock_literal_is_no_longer_a_fallback():
    """回归保护：任何解析结果都不允许退化成字面量 "mock"。"""
    for gateway in (_ConfiguredGateway(), _UnconfiguredGateway(), None):
        try:
            name = resolve_chat_model_name(snapshot_model=None, gateway=gateway)
        except ApiError:
            continue
        assert name != "mock"
