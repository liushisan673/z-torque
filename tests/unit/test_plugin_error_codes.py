"""v1.3 §8：插件错误码集合、状态码映射与审计动作分类的契约测试。

错误码的第一消费点是"集合登记 + 测试断言"（B-15）：只要实现里新造了码而没登记，
或者登记了却没人映射 HTTP 状态，这里就会红。审计动作同理——类别映射下沉到
`app.kernel.audit.categories` 单一数据源，前端标签与后端筛选才可能一致。
"""

from app.core.errors import PLUGIN_ERROR_CODES, PLUGIN_ERROR_STATUS
from app.kernel.audit.categories import (
    CATEGORY_A2A,
    CATEGORY_AUDIT,
    CATEGORY_SANDBOX,
    DEFAULT_CATEGORY,
    audit_category,
)

EXPECTED_CODES = {
    "AI_SANDBOX_DENIED",
    "AI_SANDBOX_RESOURCE_EXCEEDED",
    "AI_SKILL_TIMEOUT",
    "AI_SKILL_FAILED",
    "AI_AUDIT_EXPORT_DENIED",
    "AI_AUDIT_EXPORT_TOO_LARGE",
    "AI_AUDIT_EXPORT_FAILED",
    "AI_AUDIT_ARCHIVE_FAILED",
    "AI_AUDIT_ARCHIVE_KEY_MISSING",
    "AI_A2A_DISABLED",
    "AI_A2A_DENIED",
    "AI_A2A_TIMEOUT",
    "AI_A2A_PEER_UNAVAILABLE",
    "AI_A2A_DEPTH_EXCEEDED",
    "AI_A2A_ROUTE_FAILED",
}


def test_plugin_error_codes_match_spec_exactly():
    assert PLUGIN_ERROR_CODES == EXPECTED_CODES


def test_every_plugin_error_code_has_http_status():
    assert set(PLUGIN_ERROR_STATUS) == PLUGIN_ERROR_CODES


def test_standard_http_status_mapping():
    # 拒绝 403 / 超限 413 / 资源 429 / 超时 504 / 上游故障 502 / 关闭 503
    assert PLUGIN_ERROR_STATUS["AI_SANDBOX_DENIED"] == 403
    assert PLUGIN_ERROR_STATUS["AI_SANDBOX_RESOURCE_EXCEEDED"] == 429
    assert PLUGIN_ERROR_STATUS["AI_AUDIT_EXPORT_TOO_LARGE"] == 413
    assert PLUGIN_ERROR_STATUS["AI_A2A_DISABLED"] == 503
    assert PLUGIN_ERROR_STATUS["AI_A2A_DEPTH_EXCEEDED"] == 400
    assert PLUGIN_ERROR_STATUS["AI_A2A_ROUTE_FAILED"] == 404
    assert PLUGIN_ERROR_STATUS["AI_A2A_TIMEOUT"] == 504


def test_spec_actions_are_categorized_from_kernel_single_source():
    expected = {
        "SANDBOX_DENIED": CATEGORY_SANDBOX,
        "SANDBOX_RESOURCE_EXCEEDED": CATEGORY_SANDBOX,
        "AUDIT_EXPORTED": CATEGORY_AUDIT,
        "AUDIT_EXPORT_FAILED": CATEGORY_AUDIT,
        "AUDIT_ARCHIVED": CATEGORY_AUDIT,
        "A2A_INVOKED": CATEGORY_A2A,
        "A2A_DENIED": CATEGORY_A2A,
        "A2A_FAILED": CATEGORY_A2A,
    }
    for action, category in expected.items():
        assert audit_category(action) == category, action


def test_unknown_action_falls_back_to_declared_default():
    # 兜底类别必须与显式常量一致：新增动作若忘记登记，只会落到 DEFAULT_CATEGORY，
    # 不会"猜"进 security / audit 这类敏感筛选。
    assert audit_category("SOMETHING_BRAND_NEW") == DEFAULT_CATEGORY
