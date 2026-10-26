"""HttpExecutor 单测：POST 透传、secret:// 凭据头、内网目标白名单（target_policy）。"""

import socket

import pytest

from app.builtin_plugins.executors import HttpExecutor


class FakeResponse:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text

    @property
    def is_success(self):
        return 200 <= self.status_code < 300


class FakeClient:
    """捕获 httpx.Client 调用参数；mock.httpx.patch 安装。"""

    calls: list = []
    response: FakeResponse = FakeResponse()

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        FakeClient.calls.append(("GET", url, kwargs))
        return FakeClient.response

    def post(self, url, **kwargs):
        FakeClient.calls.append(("POST", url, kwargs))
        return FakeClient.response


@pytest.fixture()
def fake_http(monkeypatch):
    monkeypatch.setattr("app.builtin_plugins.executors.httpx.Client", FakeClient)
    FakeClient.calls = []
    FakeClient.response = FakeResponse()
    return FakeClient


BASE_CONFIG = {
    "method": "POST",
    "url": "http://192.168.2.10:8081/pool/account/page",
    "headers": {"Accept": "application/json"},
    "queryParams": {},
    "allowed_hosts": ["192.168.2.10"],
    "target_policy": {"allow_private_network": True},
}


def test_post_body_and_query_split(fake_http):
    """POST：__body__ 作为 JSON body 透传，其余参数拼 query。"""
    config = {**BASE_CONFIG, "headers": {"Accept": "application/json", "X-Paging": "{{page}}"}}
    result = HttpExecutor().execute(config, {"__body__": {"pageNo": 1}, "page": "2"}, 5)
    assert result.status == "SUCCEEDED"
    method, url, kwargs = fake_http.calls[-1]
    assert method == "POST" and url == BASE_CONFIG["url"]
    assert kwargs["json"] == {"pageNo": 1}
    assert kwargs["params"] == {"page": "2"}  # __body__ 不进 query
    assert kwargs["headers"]["X-Paging"] == "2"


def test_put_is_rejected(fake_http):
    result = HttpExecutor().execute({**BASE_CONFIG, "method": "PUT"}, {}, 5)
    assert result.status == "FAILED" and result.error_code == "AI_HTTP_METHOD_NOT_ALLOWED"


def test_json_response_body_is_parsed_for_step_references(fake_http):
    """成功响应解析出 json 字段：组合工具步骤间可引用内层字段；非 JSON 响应为 None。"""
    # 用公网 IP 字面量避免测试环境 DNS 解析（与 BASE_CONFIG 同一地址族）
    config = {**BASE_CONFIG, "method": "GET", "url": "https://192.168.2.10/search", "target_policy": {"allow_private_network": True}}
    fake_http.response = FakeResponse(status_code=200, text='{"code":"200","location":[{"name":"宝鸡","id":"101110901"}]}')
    result = HttpExecutor().execute(config, {"location": "宝鸡"}, 5)
    assert result.status == "SUCCEEDED"
    assert result.data["json"]["location"][0]["id"] == "101110901"

    fake_http.response = FakeResponse(status_code=200, text="plain text not json")
    result = HttpExecutor().execute(config, {}, 5)
    assert result.status == "SUCCEEDED"
    assert result.data["json"] is None


def test_private_http_blocked_without_policy(fake_http):
    """未声明 allow_private_network 时内网 http 目标维持默认拦截。"""
    config = {**BASE_CONFIG, "target_policy": {}}
    result = HttpExecutor().execute(config, {}, 5)
    assert result.status == "FAILED" and result.error_code == "AI_HTTP_TARGET_BLOCKED"


def test_secret_ref_header_is_decrypted(fake_http, monkeypatch):
    """headers 值 secret:// 开头 → SecretStore 解密后随请求发送；明文不落库。"""
    from app.security.secret_store import SecretStoreError

    class FakeStore:
        def decrypt(self, value):
            if value == "good-cipher":
                return "tok-csrf"
            raise SecretStoreError("bad")

    monkeypatch.setattr("app.security.secret_store.SecretStore", FakeStore)
    config = {**BASE_CONFIG, "headers": {"CSRF-TOKEN": "secret://good-cipher", "X-AUTH-TOKEN": "secret://good-cipher"}}
    result = HttpExecutor().execute(config, {}, 5)
    assert result.status == "SUCCEEDED"
    _, _, kwargs = fake_http.calls[-1]
    assert kwargs["headers"]["CSRF-TOKEN"] == "tok-csrf"
    assert kwargs["headers"]["X-AUTH-TOKEN"] == "tok-csrf"


def test_bad_secret_ref_fails_with_template_error(fake_http, monkeypatch):
    from app.security.secret_store import SecretStoreError

    class FakeStore:
        def decrypt(self, value):
            raise SecretStoreError("bad")

    monkeypatch.setattr("app.security.secret_store.SecretStore", FakeStore)
    config = {**BASE_CONFIG, "headers": {"CSRF-TOKEN": "secret://broken"}}
    result = HttpExecutor().execute(config, {}, 5)
    assert result.status == "FAILED" and result.error_code == "AI_HTTP_TEMPLATE_UNRESOLVED"


def test_allowed_url_matrix(monkeypatch):
    """https 公网目标经 DNS 解析判定 is_global；固定解析结果，不依赖真实外网 DNS。"""

    def _fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]

    monkeypatch.setattr("socket.getaddrinfo", _fake_getaddrinfo)
    allow = HttpExecutor._allowed_url
    # 白名单内 https 公网（用解析得到的 IP 判定；localhost 私网回环恒被拒）
    assert allow("https://example.com/x", ["example.com"]) is True
    assert allow("http://example.com/x", ["example.com"]) is False  # 无声明时 http 明文拒绝
    assert allow("http://192.168.2.10:8081/x", ["192.168.2.10"], True) is True  # 显式放行内网
    assert allow("http://192.168.2.10:8081/x", ["192.168.2.10"]) is False
    assert allow("https://evil.com/x", ["example.com"]) is False  # 白名单外
    assert allow("https://example.com/x", []) is False  # 空白名单 fail-closed
    assert allow("ftp://example.com/x", ["example.com"], True) is False  # 非 http(s) scheme
