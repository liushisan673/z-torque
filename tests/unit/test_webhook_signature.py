"""入站 Webhook 签名校验：算法/编码/模板/时间窗与 Token 模式。"""

import base64
import hashlib
import hmac
import time

from app.services.webhook_signature import compute_signature, verify_signature, verify_token


def test_default_hmac_sha256_hex():
    body = b'{"text":"hi"}'
    ts = "1700000000"
    signature = compute_signature("secret", timestamp=ts, raw_body=body)
    expected = "sha256=" + hmac.new(b"secret", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert signature == expected
    assert verify_signature(
        secret="secret", timestamp=ts, signature=signature, raw_body=body, tolerance_seconds=10**9
    )


def test_custom_sha1_base64_template_and_prefix():
    body = b'{"a":1}'
    ts = "1700000000"
    kwargs = dict(algorithm="SHA1", encoding="BASE64", prefix="v1=", template="{timestamp}:{body}")
    signature = compute_signature("secret", timestamp=ts, raw_body=body, **kwargs)
    expected = "v1=" + base64.b64encode(
        hmac.new(b"secret", f"{ts}:".encode() + body, hashlib.sha1).digest()
    ).decode()
    assert signature == expected
    assert verify_signature(
        secret="secret", timestamp=ts, signature=signature, raw_body=body, tolerance_seconds=10**9, **kwargs
    )


def test_body_only_template_does_not_require_timestamp():
    body = b'{"a":1}'
    signature = compute_signature("secret", timestamp="", raw_body=body, prefix="", template="{body}")
    assert signature == hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature(
        secret="secret", timestamp="", signature=signature, raw_body=body, prefix="", template="{body}"
    )


def test_stale_timestamp_is_rejected():
    body = b"{}"
    old = str(int(time.time()) - 600)
    signature = compute_signature("secret", timestamp=old, raw_body=body)
    assert not verify_signature(secret="secret", timestamp=old, signature=signature, raw_body=body, tolerance_seconds=300)


def test_secret_is_actually_used():
    body = b"{}"
    ts = "1700000000"
    assert compute_signature("a", timestamp=ts, raw_body=body) != compute_signature("b", timestamp=ts, raw_body=body)


def test_token_verification_with_prefix():
    assert verify_token("secret", "Bearer secret", "Bearer ")
    assert verify_token("secret", "secret", "")
    assert not verify_token("secret", "Bearer nope", "Bearer ")
    assert not verify_token("secret", "nope", "")
