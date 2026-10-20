"""连接器级凭据头（auth_headers）测试：保存转换、出参掩码、执行期合并覆盖、探测解密。"""

from types import SimpleNamespace

from app.api.routes.connectors import _rest_auth_headers, _sanitize_config_for_output
from app.kernel.skill_gateway import SkillGateway
from app.security.secret_store import SecretStore


def _ref(plain: str) -> str:
    return f"secret://{SecretStore().encrypt(plain)}"


def test_apply_auth_headers_decrypts_and_merges():
    config = {"auth_mode": "Bearer Token", "auth_headers": {"CSRF-TOKEN": _ref("csrf-v1"), "X-AUTH-TOKEN": _ref("auth-v1")}}
    headers, used = _rest_auth_headers(config, _ref("bearer-v1"))
    assert used is True
    assert headers["CSRF-TOKEN"] == "csrf-v1"
    assert headers["X-AUTH-TOKEN"] == "auth-v1"
    assert headers["Authorization"] == "Bearer bearer-v1"


def test_apply_auth_headers_without_secret_ref_still_applies_headers():
    config = {"auth_mode": "匿名", "auth_headers": {"CSRF-TOKEN": _ref("csrf-v1")}}
    headers, used = _rest_auth_headers(config, None)
    assert headers == {"CSRF-TOKEN": "csrf-v1"}
    assert used is True


def test_sanitize_output_masks_auth_headers():
    out = _sanitize_config_for_output({"auth_headers": {"CSRF-TOKEN": _ref("csrf-v1")}})
    masked = out["auth_headers"]["CSRF-TOKEN"]
    assert masked.startswith("secret://••••")
    assert "csrf-v1" not in masked


def test_materialize_merges_connector_auth_headers_over_templates():
    """连接器凭据头覆盖导入工具同名 {{参数}} 模板项；非同名工具头保留。"""
    csrf_ref, auth_ref = _ref("csrf-live"), _ref("auth-live")
    gateway = SkillGateway()
    tool = SimpleNamespace(
        config_snapshot={
            "method": "POST",
            "url": "http://192.168.2.10:8081/user-model/page",
            "headers": {"Accept": "application/json", "CSRF-TOKEN": "{{CSRF-TOKEN}}", "X-AUTH-TOKEN": "{{X-AUTH-TOKEN}}"},
        },
    )
    connector = SimpleNamespace(
        connector_type="REST",
        config={"auth_headers": {"CSRF-TOKEN": csrf_ref, "X-AUTH-TOKEN": auth_ref}},
        secret_ref=None,
        target_policy={},
    )
    config = gateway._materialize_config(tool, connector)
    assert config["headers"]["CSRF-TOKEN"] == csrf_ref  # 凭据引用覆盖模板
    assert config["headers"]["X-AUTH-TOKEN"] == auth_ref
    assert config["headers"]["Accept"] == "application/json"  # 非同名保留


def test_materialize_without_auth_headers_keeps_templates():
    gateway = SkillGateway()
    tool = SimpleNamespace(config_snapshot={"headers": {"CSRF-TOKEN": "{{CSRF-TOKEN}}"}})
    connector = SimpleNamespace(connector_type="REST", config={}, secret_ref=None, target_policy={})
    config = gateway._materialize_config(tool, connector)
    assert config["headers"]["CSRF-TOKEN"] == "{{CSRF-TOKEN}}"
