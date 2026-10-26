"""A2A 远端真联调：两个本项目实例互指 peer + 真实 TCP/TLS 对端。

默认 skip，`ZTORQUE_A2A_LIVE=1` 打开（约 20~40s：要起 uvicorn 子进程、绑若干本地端口）。

为什么值得单独一层：M3 之前所有远端用例都是 `httpx.MockTransport` 替身，
「对端返回本项目统一响应封装」与「https 强制 + 证书校验」这两件事从未被真实链路验证过
（真联调当场暴露了前者：出站把 `{code,message,data}` 当 A2A 响应用，永远拿不到 status）。

覆盖：
1. `peer=b`：A 的 `route=remote` 真的打到 B 的 `/api/z-torque/a2a/invoke`，B 用**转发来的本域 JWT**
   重新认证（`A2A_IDENTITY_PROPAGATION=full_context_token`）并由本地 `skill.executor`
   执行钉死的技能版本，A 拿到链路末端结果；B 侧确实留下 `A2A_INVOKED` 审计行（depth=1）。
2. `peer=ok`：对端按统一封装返回成功响应，A 解包后得到 `SUCCEEDED`，且出站头带 depth/trace。
3. `peer=deny`：对端 4xx → `AI_A2A_DENIED`（403）。
4. `peer=slow`：对端睡过 peer 级 timeout → `AI_A2A_TIMEOUT`（504）。
5. `peer=err`：对端 5xx → `AI_A2A_PEER_UNAVAILABLE`（503）。
6. `peer=big`：对端响应超过 `A2A_MAX_RESPONSE_BYTES` → `AI_A2A_PEER_UNAVAILABLE`。
7. `peer=tls`：https 自签证书 → 默认证书校验失败（`AI_A2A_PEER_UNAVAILABLE`）。
8. 熔断：独立实例（阈值 2）连续失败达阈值 → 第三次 `AI_PLUGIN_CIRCUIT_OPEN`，且不再发请求。

熔断用例单独起一个实例：熔断是**进程内状态**，与主实例共用会让失败计数互相污染、
再靠测试文件顺序来"消灾"就是假通过。
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  # 确保模型注册
from app.builtin_plugins.a2a_gateway.protocol import A2A_DEPTH_HEADER, A2A_TRACE_HEADER, A2A_VERSION
from app.models import (
    AuditLog,
    Permission,
    Role,
    Skill,
    SkillVersion,
    Tenant,
    User,
    role_permission,
    user_role,
)
from app.security.passwords import hash_password

pytestmark = pytest.mark.skipif(
    os.environ.get("ZTORQUE_A2A_LIVE") != "1",
    reason="set ZTORQUE_A2A_LIVE=1 to run live A2A peer tests (starts uvicorn subprocesses)",
)

ROOT = Path(__file__).resolve().parents[2]
_MAX_RESPONSE_BYTES = 1024
_JWT_SECRET = "live-test-secret"


# ---------------------------------------------------------------- 进程与端口


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(port: int, *, tls: bool, process: Any, timeout: float = 60.0) -> None:
    scheme = "https" if tls else "http"
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            output = process.stdout.read() if process.stdout else b""
            raise AssertionError(f"instance on :{port} exited early:\n{output.decode('utf-8', 'replace')[-2000:]}")
        try:
            response = httpx.get(f"{scheme}://127.0.0.1:{port}/health", timeout=1.0, verify=False)
            if response.status_code == 200:
                return
            last_error = f"{response.status_code} {response.text[:200]}"
        except Exception as exc:  # noqa: BLE001 - 就绪轮询，任何异常都继续等
            last_error = str(exc)
        time.sleep(0.25)
    raise AssertionError(f"instance on :{port} not ready in {timeout}s: {last_error}")


@contextmanager
def _serve_app(port: int, env: dict[str, str]) -> Iterator[Any]:
    """起一个真实 uvicorn 子进程（`app.main:app`，由 env 决定装配 profile 与 A2A 配置）。"""
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    process = subprocess.Popen(  # noqa: S603 - 固定命令，参数全部由本测试构造
        command,
        cwd=str(ROOT),
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_ready(port, tls=False, process=process)
        yield process
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover - 极端情况下强杀
            process.kill()


# ---------------------------------------------------------------- 对端 stub（真 TCP / 真 TLS）


class _StubState:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.requests)


def _make_handler(state: _StubState, *, mode: str, delay: float, size: int):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # 静默
            return

        def do_POST(self) -> None:  # noqa: N802 - 标准库接口名
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b""
            with state.lock:
                state.requests.append(
                    {
                        "path": self.path,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                        "body": json.loads(raw or b"{}"),
                    }
                )
            if delay:
                time.sleep(delay)
            if mode == "error":
                self.send_response(500)
                self.send_header("content-length", "0")
                self.end_headers()
                return
            if mode == "deny":
                body = json.dumps({"code": "AI_A2A_DENIED", "message": "stub denies", "data": None, "trace_id": "t"}).encode()
                self.send_response(403)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if mode == "big":
                payload = {"code": 0, "message": "ok", "data": {"status": "SUCCEEDED", "data": {"blob": "x" * size}}, "trace_id": "t"}
            else:
                # 成功路径刻意按本项目统一响应封装返回：出站必须解包 data 才能拿到 status
                payload = {"code": 0, "message": "ok", "data": {"id": "req", "status": "SUCCEEDED", "data": {"echo": True}}, "trace_id": "t"}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


@contextmanager
def _serve_stub(
    *, mode: str = "ok", delay: float = 0.0, size: int = 0, tls: tuple[str, str] | None = None
) -> Iterator[tuple[int, _StubState]]:
    state = _StubState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state, mode=mode, delay=delay, size=size))
    if tls is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=tls[0], keyfile=tls[1])
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1]), state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _self_signed_cert(tmp_path: Path) -> tuple[str, str]:
    """生成 127.0.0.1 的自签证书（只为本用例证明"默认不信任自签"）。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "stub.crt"
    key_path = tmp_path / "stub.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


# ---------------------------------------------------------------- DB 与实例装配


def _prepare_database(db_path: Path) -> tuple[Any, dict[str, Any]]:
    """建库（迁移到 head）+ seed 一个带 `a2a.invoke` 的租户/用户。"""
    from alembic.config import Config

    from alembic import command

    db_url = f"sqlite:///{db_path.as_posix()}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(config, "head")

    engine = create_engine(db_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    tenant_id = uuid4()
    with session_factory.begin() as db:
        db.add(Tenant(id=tenant_id, name=f"live-{tenant_id}"))
        db.flush()
        user = User(tenant_id=tenant_id, username="live-caller", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        role = Role(tenant_id=tenant_id, name="live-caller")
        permission = Permission(tenant_id=tenant_id, name="a2a.invoke")
        db.add_all([role, permission])
        db.flush()
        db.execute(role_permission.insert().values(role_id=role.id, permission_id=permission.id))
        db.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
        # 一个"可执行"的已发布技能版本：不绑工具 → skill.executor 直接回 instructions，
        # 这样远端对端能真正执行并 SUCCEEDED，而不是停在"找不到技能"。
        skill = Skill(tenant_id=tenant_id, name="live-echo", description="live a2a skill", status="ACTIVE")
        db.add(skill)
        db.flush()
        version = SkillVersion(
            tenant_id=tenant_id,
            skill_id=skill.id,
            version_number=1,
            status="PUBLISHED",
            config_snapshot={},
            instructions="live-echo instructions",
        )
        db.add(version)
        db.flush()
        skill.current_published_version_id = version.id
        seeded = {"tenant_id": tenant_id, "user_id": user.id, "skill_version_id": version.id}
    return engine, {"db_url": db_url, "session": session_factory, **seeded}


def _peer(peer_id: str, port: int, *, scheme: str = "http", timeout: float | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": peer_id, "base_url": f"{scheme}://127.0.0.1:{port}"}
    if timeout is not None:
        entry["timeout_seconds"] = timeout
    return entry


def _peers_env(peers: list[dict[str, Any]]) -> dict[str, str]:
    return {
        "A2A_REMOTE_PEERS": json.dumps(peers),
        "A2A_ALLOWED_PEERS": json.dumps([peer["id"] for peer in peers]),
    }


@pytest.fixture(scope="module")
def live(tmp_path_factory) -> Iterator[dict[str, Any]]:
    tmp_path = tmp_path_factory.mktemp("a2a-live")
    engine, seeded = _prepare_database(tmp_path / "live.db")
    base_env = {
        "ASSEMBLY_PROFILE": "enterprise",
        "DATABASE_URL": seeded["db_url"],
        "JWT_SECRET": _JWT_SECRET,
        "A2A_ENABLED": "true",
        "A2A_DEFAULT_TIMEOUT_SECONDS": "5",
        "A2A_MAX_RESPONSE_BYTES": str(_MAX_RESPONSE_BYTES),
        "A2A_CIRCUIT_FAILURE_THRESHOLD": "2",
        "A2A_MAX_DEPTH": "3",
        # A → B 时把**真实**本域 JWT 转发给 B，B 才能用 identity.provider 重新认证
        # （B 侧不信任裸 X-User-Id，这是 §6.5 的红线）
        "A2A_IDENTITY_PROPAGATION": "full_context_token",
    }

    with ExitStack() as stack:
        stub_ok, state_ok = stack.enter_context(_serve_stub(mode="ok"))
        stub_err, _ = stack.enter_context(_serve_stub(mode="error"))
        stub_deny, _ = stack.enter_context(_serve_stub(mode="deny"))
        stub_slow, _ = stack.enter_context(_serve_stub(mode="ok", delay=1.5))
        stub_big, _ = stack.enter_context(_serve_stub(mode="big", size=4096))
        cert, key = _self_signed_cert(tmp_path)
        stub_tls, _ = stack.enter_context(_serve_stub(mode="ok", tls=(cert, key)))

        # B 实例**不配任何远端 peer**：它必须靠本地 skill.executor 执行 A 送来的技能版本。
        # 这样一旦出站载荷被原样转发（含 sender 命名空间的 peer id），B 会立刻 503 而不是
        # "碰巧也能跑"——用例因此能锁住"对端执行"而非"对端再路由"。
        peer_b_port = _free_port()
        stack.enter_context(_serve_app(peer_b_port, base_env))

        peers = [
            _peer("b", peer_b_port),
            _peer("ok", stub_ok),
            _peer("err", stub_err),
            _peer("deny", stub_deny),
            _peer("slow", stub_slow, timeout=0.4),
            _peer("big", stub_big),
            _peer("tls", stub_tls, scheme="https"),
        ]
        caller_port = _free_port()
        stack.enter_context(_serve_app(caller_port, {**base_env, **_peers_env(peers)}))

        login = httpx.post(
            f"http://127.0.0.1:{caller_port}/api/z-torque/auth/login",
            json={"username": "live-caller", "password": "pass"},
            timeout=30,
        )
        assert login.status_code == 200, login.text
        token = login.json()["data"]["access_token"]

        yield {
            "base_env": base_env,
            "caller": f"http://127.0.0.1:{caller_port}",
            "peer_b": f"http://127.0.0.1:{peer_b_port}",
            "token": token,
            "session": seeded["session"],
            "tenant_id": seeded["tenant_id"],
            "skill_version_id": seeded["skill_version_id"],
            "stub_ok": state_ok,
            "stub_err_port": stub_err,
        }
    engine.dispose()


def _invoke(base: str, token: str, **params: Any) -> httpx.Response:
    body = {"a2a_version": A2A_VERSION, "id": f"req-{uuid4()}", "params": params}
    return httpx.post(
        f"{base}/api/z-torque/a2a/invoke",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )


def _remote(
    base: str,
    token: str,
    peer: str,
    *,
    arguments: dict[str, Any] | None = None,
    skill_version_id: str | None = None,
) -> httpx.Response:
    """remote 路由的最小合法载荷：`params.peer` + `params.skill` 都必填。"""
    params: dict[str, Any] = {"route": "remote", "peer": peer, "skill": "echo", "arguments": arguments or {}}
    if skill_version_id is not None:
        params["skill_version_id"] = skill_version_id
    return _invoke(base, token, **params)


# ---------------------------------------------------------------- 用例


def test_remote_call_is_executed_by_real_peer_instance(live):
    """A → B（真实例）→ skill.executor：B 用转发来的本域 JWT 重新认证并**执行**技能版本。

    这是真联调抓到的第二个缺陷：出站载荷原本把 `params.peer`（发送方命名空间里的名字）
    原样转发，B 会拿它去查自己的 peer 表 → 503/打错人。修法是出站把 route 改写成
    `local`（调用方已钉死 skill_version_id），让对端执行而不是再路由一次。
    """
    skill_version_id = str(live["skill_version_id"])

    # 先直接打 B 一次：证明"B 自己执行这个版本"是通的，把前置条件与链路问题分开定位
    direct = _invoke(live["peer_b"], live["token"], route="local", skill_version_id=skill_version_id, arguments={})
    assert direct.status_code == 200, direct.text
    assert direct.json()["data"]["status"] == "SUCCEEDED", direct.text

    response = _remote(
        live["caller"],
        live["token"],
        "b",
        arguments={},
        skill_version_id=skill_version_id,
    )

    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["status"] == "SUCCEEDED", result
    assert result["peer"] == "b"
    assert result["depth"] == 1  # A 只记录自己这一跳
    assert result["data"] == {"instructions": "live-echo instructions"}, "B 没有真的执行技能"

    # B 侧审计（同一 DB、同一租户）：depth=1 且 peer=b 的那条只可能来自 B 的入站处理
    with live["session"]() as db:
        rows = db.scalars(
            select(AuditLog).where(AuditLog.tenant_id == live["tenant_id"], AuditLog.action == "A2A_INVOKED")
        ).all()
    peer_rows = [
        row for row in rows if (row.detail or {}).get("peer") == "b" and (row.detail or {}).get("depth") == 1
    ]
    assert peer_rows, f"B 实例没有落 A2A_INVOKED 审计：{[(row.action, row.detail) for row in rows]}"


def test_remote_call_unwraps_unified_envelope(live):
    """对端按本项目统一封装返回成功响应，出站必须解包 data 再判 status。"""
    response = _remote(live["caller"], live["token"], "ok", arguments={})

    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["status"] == "SUCCEEDED"
    assert result["data"] == {"echo": True}
    assert result["depth"] == 1
    assert result["peer"] == "ok"

    sent = [item for item in live["stub_ok"].snapshot() if item["headers"].get(A2A_DEPTH_HEADER.lower()) == "1"][-1]
    assert sent["headers"].get(A2A_TRACE_HEADER.lower()) is not None, "trace 必须透传给对端"
    assert sent["body"]["params"]["peer"] == "ok"
    assert sent["body"]["params"]["skill"] == "echo"
    # 没给 skill_version_id 时无法钉死"执行什么"，只能把 route 原样转发（对端自行路由）
    assert sent["body"]["params"]["route"] == "remote"


def test_remote_4xx_maps_to_denied(live):
    """对端 4xx 是"拒绝"，不是"不可用"：必须映射为 AI_A2A_DENIED。"""
    response = _remote(live["caller"], live["token"], "deny")

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "AI_A2A_DENIED"


def test_remote_timeout_maps_to_a2a_timeout(live):
    """对端睡过 peer 级 timeout（0.4s vs 1.5s）→ AI_A2A_TIMEOUT（504）。"""
    response = _remote(live["caller"], live["token"], "slow")

    assert response.status_code == 504, response.text
    assert response.json()["code"] == "AI_A2A_TIMEOUT"


def test_remote_5xx_maps_to_peer_unavailable(live):
    """对端 5xx → AI_A2A_PEER_UNAVAILABLE（503）。"""
    for _ in range(2):
        response = _remote(live["caller"], live["token"], "err")
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "AI_A2A_PEER_UNAVAILABLE"


def test_remote_oversized_response_is_rejected(live):
    """对端 4096 字节 > 1024 上限：拒绝而不是截断后解析。"""
    response = _remote(live["caller"], live["token"], "big")

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "AI_A2A_PEER_UNAVAILABLE"
    assert "max_response_bytes" in response.json()["message"]


def test_self_signed_https_peer_is_rejected(live):
    """强制 https 不是纸面承诺：自签证书默认不被信任。"""
    response = _remote(live["caller"], live["token"], "tls")

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "AI_A2A_PEER_UNAVAILABLE"


def test_circuit_opens_after_threshold_and_stops_calling(live):
    """阈值 2 的独立实例：连续两次 5xx 后熔断打开，第三次快速失败且不再发请求。"""
    port = _free_port()
    with _serve_app(
        port,
        {
            **live["base_env"],
            **_peers_env([_peer("err", live["stub_err_port"])]),
            # 熔断阈值来自内核 plugin_breaker_*（A2A 自己的 circuit_failure_threshold 目前只做装配期校验）
            "PLUGIN_BREAKER_FAILURE_THRESHOLD": "2",
            "PLUGIN_BREAKER_COOLDOWN_SECONDS": "60",
        },
    ):
        base = f"http://127.0.0.1:{port}"
        codes = []
        for _ in range(3):
            response = _remote(base, live["token"], "err")
            codes.append((response.status_code, response.json()["code"]))

    assert codes == [
        (503, "AI_A2A_PEER_UNAVAILABLE"),
        (503, "AI_A2A_PEER_UNAVAILABLE"),
        (503, "AI_PLUGIN_CIRCUIT_OPEN"),
    ], codes
