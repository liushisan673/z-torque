"""内置工具执行器插件（HTTP / DB_QUERY / MD_SCRIPT；MCP 经网关协商执行）。

MOCK 执行器已从生产移除；测试替身见 tests/support/fakes.py。
测试约定：无条件成功的执行器只能由测试 DI 局部 register，禁止回挂生产装配
（app/builtin_plugins/plugins.py）。

自 app/kernel/builtin_executors.py 与 app/kernel/skill_gateway.py 迁移，逻辑不变；
执行器契约见 app/kernel/ports/skill_executor.py。
DB_SQL（连接器目标库执行）仍在 app/kernel/db_executor.py（含注册期 SQL 校验纯函数），
由装配根直接引用。
"""

import ipaddress
import json
import os
import re
import socket
import time
from typing import Any
from urllib.parse import urlparse

import httpcore
import httpx
from sqlalchemy import Engine, text
from sqlglot import exp, parse

from app.kernel.md_skill_runtime import MdSkillRuntime
from app.kernel.ports.skill_executor import ExecutionResult

# HTTP 工具方法白名单：注册入口（OpenAPI 导入 / 手动注册）与执行器引用同一来源，
# 将来放开新方法时改这里即可，两个闸门不会漂移
SUPPORTED_HTTP_METHODS = ("GET", "POST")

_TEMPLATE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")

_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _global_proxy_configured() -> bool:
    """全局出站代理是否已配置（PROXY_URL 会被写入进程环境，故统一看环境变量）。"""
    return any(os.environ.get(key) for key in _PROXY_ENV_KEYS)


class _PinnedBackend(httpcore.SyncBackend):
    """把 TCP 连接固定到校验过的 IP，同时保留原主机名用于 TLS SNI 与证书校验。

    httpx/httpcore 默认在连接时再次解析域名：若只做前置 getaddrinfo 校验，攻击者可在
    校验与连接之间改变 DNS 结果（DNS rebinding TOCTOU）。这里在连接层改写目标地址，
    域名仅参与 TLS 校验，从根上消除二次解析。
    """

    def __init__(self, host: str, ip: str) -> None:
        super().__init__()
        self._host = host
        self._ip = ip

    def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None, socket_options=None):
        if host != self._host:
            raise httpcore.ConnectError("connection host differs from the validated host")
        return super().connect_tcp(self._ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options)


class HttpExecutor:
    tool_type = "HTTP"

    def execute(self, config: dict, arguments: dict, timeout_seconds: float) -> ExecutionResult:
        method = str(config.get("method", "GET")).upper()
        url = str(config.get("url", ""))
        if method not in SUPPORTED_HTTP_METHODS:
            return ExecutionResult(status="FAILED", error_code="AI_HTTP_METHOD_NOT_ALLOWED", error_message="only GET and POST are enabled")
        secret = config.get("secret_value")
        try:
            headers = {str(key): self._resolve(self._resolve_credential(str(value)), arguments, secret) for key, value in (config.get("headers") or {}).items()}
            query_params = {str(key): self._resolve(self._resolve_credential(str(value)), arguments, secret) for key, value in (config.get("queryParams") or {}).items()}
        except ValueError as exc:
            return ExecutionResult(status="FAILED", error_code="AI_HTTP_TEMPLATE_UNRESOLVED", error_message=str(exc))
        # 身份透传（P2-5：SecurityContext 贯穿外部调用）：保留键 _identity 由网关注入，
        # 仅执行期存在；下游系统可经 X-ZT-* 头审计委托链
        identity = config.get("_identity") if isinstance(config.get("_identity"), dict) else None
        if identity:
            if identity.get("user_id"):
                headers["X-ZT-User-Id"] = str(identity["user_id"])
            if identity.get("trace_id"):
                headers["X-ZT-Trace-Id"] = str(identity["trace_id"])
            if identity.get("task_id"):
                headers["X-ZT-Task-Id"] = str(identity["task_id"])
        # 连接器显式声明 allow_private_network（经 target_policy）时放行内网 http 目标；
        # 未声明维持默认 SSRF 收敛（https + 公网）。host 始终必须在 allowed_hosts 白名单内。
        target_policy = config.get("target_policy") if isinstance(config.get("target_policy"), dict) else {}
        allow_private = bool(config.get("allow_private_network") or target_policy.get("allow_private_network"))
        target = self._validated_target(url, config.get("allowed_hosts", []), allow_private)
        if target is None:
            return ExecutionResult(status="FAILED", error_code="AI_HTTP_TARGET_BLOCKED", error_message="HTTP target is not allowed")
        host, pinned_ip = target
        # 出站代理：连接器 config.proxy 三态——缺省=跟随全局（环境变量/PROXY_URL）；
        # ""=强制直连；URL=指定代理。指定代理时由代理解析目标，不再固定 IP（配置者担责）。
        proxy = config.get("proxy") if "proxy" in config else None
        if isinstance(proxy, str):
            proxy = proxy.strip()
            if proxy and not (proxy.startswith("http://") or proxy.startswith("https://")):
                return ExecutionResult(status="FAILED", error_code="AI_HTTP_CONFIG_INVALID", error_message="proxy must be an http(s) proxy URL or empty string")
        try:
            client = self._client(timeout_seconds, host, pinned_ip, proxy if isinstance(proxy, str) else None)
        except RuntimeError:
            # 无法把连接固定到已校验地址时 fail-closed，避免退化为可被 rebinding 绕过的校验
            return ExecutionResult(status="FAILED", error_code="AI_HTTP_TARGET_BLOCKED", error_message="HTTP client cannot pin the validated address")
        try:
            max_response_bytes = max(1, min(int(config.get("max_response_bytes", 1_000_000)), 10_000_000))
            request_kwargs: dict[str, Any] = {"headers": headers}
            if method == "POST":
                # 导入工具约定：requestBody 整体作为 __body__ 参数；其余参数拼 query
                request_kwargs["json"] = arguments.get("__body__")
                query_args = {key: value for key, value in arguments.items() if key != "__body__"}
                request_kwargs["params"] = {**query_params, **query_args}
            else:
                request_kwargs["params"] = {**query_params, **arguments}

            # 使用流式响应；旧实现 response.text 会先完整缓冲响应，再切片，
            # max_response_bytes 只是截断展示，并不能限制内存占用。
            with client:
                with client.stream(method, url, **request_kwargs) as response:
                    response_status = response.status_code
                    response_success = response.is_success
                    content_length = response.headers.get("content-length", "")
                    if content_length.isdigit() and int(content_length) > max_response_bytes:
                        return ExecutionResult(
                            status="FAILED",
                            error_code="AI_HTTP_REQUEST_FAILED",
                            error_message="HTTP response exceeded configured size limit",
                        )
                    body_bytes = bytearray()
                    exceeded = False
                    for chunk in response.iter_bytes():
                        remaining = max_response_bytes - len(body_bytes)
                        if len(chunk) > remaining:
                            body_bytes.extend(chunk[:remaining])
                            exceeded = True
                            break
                        body_bytes.extend(chunk)
                    if exceeded:
                        return ExecutionResult(
                            status="FAILED",
                            error_code="AI_HTTP_REQUEST_FAILED",
                            error_message="HTTP response exceeded configured size limit",
                        )
                    body = bytes(body_bytes).decode(response.encoding or "utf-8", errors="replace")

            # 能解析为 JSON 时附加 json 字段，组合工具步骤间即可引用内层字段。
            data: dict[str, Any] = {"status_code": response_status, "body": body}
            try:
                data["json"] = json.loads(body)
            except ValueError:
                data["json"] = None
            return ExecutionResult(
                status="SUCCEEDED" if response_success else "FAILED",
                data=data,
                error_code=None if response_success else "AI_HTTP_ERROR",
            )
        except (httpx.HTTPError, ValueError) as exc:
            return ExecutionResult(status="FAILED", error_code="AI_HTTP_REQUEST_FAILED", error_message=str(exc))

    @staticmethod
    def _resolve_credential(value: str) -> str:
        """凭据引用：secret:// 开头的值经 SecretStore 解密（明文不落库），其余原样返回。"""
        if value.startswith("secret://"):
            from app.security.secret_store import SecretStore, SecretStoreError

            try:
                return SecretStore().decrypt(value.removeprefix("secret://"))
            except SecretStoreError as exc:
                raise ValueError("tool credential secret reference cannot be decrypted") from exc
        return value

    @staticmethod
    def _resolve(value: str, arguments: dict, secret: str | None) -> str:
        """解析配置模板：{{参数名}} 取工具调用参数，{{secret}} 取连接器密钥明文。"""

        def substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            if key == "secret":
                if not secret:
                    raise ValueError("placeholder {{secret}} referenced but the connector secret is not configured")
                return secret
            if key in arguments:
                return str(arguments[key])
            raise ValueError(f"template placeholder {{{{{key}}}}} cannot be resolved")

        return _TEMPLATE.sub(substitute, value)

    @staticmethod
    def _client(timeout_seconds: float, host: str, pinned_ip: str | None, proxy: str | None = None) -> httpx.Client:
        """构造 HTTP 客户端；公网目标把连接钉在已校验 IP 上（pinned_ip=None 表示显式放行内网）。

        proxy：None=跟随全局（配置了全局代理则走代理，否则直连）；""=强制直连；URL=指定代理。
        """
        if proxy:
            # 显式代理：连接由代理解析，pinned IP 无意义；trust_env=False 避免与环境变量叠加
            return httpx.Client(proxy=proxy, trust_env=False, follow_redirects=False, timeout=timeout_seconds)
        if proxy == "":
            # 强制直连：内网目标本就无 transport 固定，公网目标继续用 pinned transport
            if pinned_ip is None:
                return httpx.Client(follow_redirects=False, timeout=timeout_seconds, trust_env=False)
        elif pinned_ip is not None and _global_proxy_configured():
            # 跟随全局且确实配置了代理：交给 httpx 环境代理（NO_PROXY 内的目标仍直连），
            # 与探测路径保持一致；代价是放弃 IP 固定（由配置者经 PROXY_URL 显式开启）
            return httpx.Client(follow_redirects=False, timeout=timeout_seconds, trust_env=True)
        if pinned_ip is None:
            return httpx.Client(follow_redirects=False, timeout=timeout_seconds)
        transport = httpx.HTTPTransport(retries=0)
        try:
            transport._pool._network_backend = _PinnedBackend(host, pinned_ip)
        except AttributeError as exc:  # httpx 内部结构变化：宁可拒绝也不静默退化
            transport.close()
            raise RuntimeError("http transport cannot be pinned") from exc
        return httpx.Client(transport=transport, follow_redirects=False, timeout=timeout_seconds)

    @staticmethod
    def _validated_target(url: str, allowed_hosts: list[str], allow_private_network: bool = False) -> tuple[str, str | None] | None:
        """校验目标并返回 (host, 待固定 IP)；None 表示拒绝。

        返回的 IP 由调用方传给 _PinnedBackend，保证连接阶段不再解析域名。
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.hostname not in set(allowed_hosts):
            return None
        if allow_private_network:
            # 显式放行：连接器 target_policy 声明的内网/明文目标由配置者担责
            return parsed.hostname, None
        if parsed.scheme != "https":
            return None
        try:
            addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        except (OSError, ValueError):
            return None
        resolved: list[str] = []
        for item in addresses:
            try:
                address = ipaddress.ip_address(item[4][0])
            except ValueError:
                return None
            # 回环/私网/链路本地/保留段全部拒绝（比 is_private 更严格，覆盖 CGNAT 等）
            if not address.is_global:
                return None
            resolved.append(str(address))
        if not resolved:
            return None
        return parsed.hostname, resolved[0]

    @staticmethod
    def _allowed_url(url: str, allowed_hosts: list[str], allow_private_network: bool = False) -> bool:
        return HttpExecutor._validated_target(url, allowed_hosts, allow_private_network) is not None


class DbQueryExecutor:
    tool_type = "DB_QUERY"

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def execute(self, config: dict, arguments: dict, timeout_seconds: float) -> ExecutionResult:
        query = str(config.get("query", "")).strip()
        try:
            dialect = self._dialect()
            statements = parse(query, read=dialect)
        except Exception:
            return ExecutionResult(status="FAILED", error_code="AI_REQUEST_INVALID", error_message="query parse failed")
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            return ExecutionResult(status="FAILED", error_code="AI_REQUEST_INVALID", error_message="exactly one SELECT statement is required")
        statement = statements[0]
        allowed_tables = set(config.get("allowed_tables", []))
        allowed_columns = set(config.get("allowed_columns", []))
        if not self._authorized_references(statement, allowed_tables, allowed_columns):
            return ExecutionResult(status="FAILED", error_code="AI_FORBIDDEN", error_message="query references an unauthorized table, column, or operation")

        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        try:
            max_rows = max(1, min(int(config.get("max_rows", 100)), 1000))
            timeout_ms = max(100, min(int(timeout_seconds * 1000), 60000))
            # 外层 LIMIT 使数据库本身也受行数上限约束，而不只是 Python fetchmany 截断。
            safe_query = f"SELECT * FROM ({statement.sql(dialect=dialect)}) AS zt_query_result LIMIT {max_rows}"
            deadline = time.monotonic() + (timeout_ms / 1000)
            with self.engine.begin() as connection:
                raw_connection = None
                if self.engine.dialect.name == "sqlite":
                    raw_connection = connection.connection.driver_connection
                    raw_connection.set_progress_handler(
                        lambda: 1 if time.monotonic() >= deadline else 0,
                        1000,
                    )
                elif self.engine.dialect.name == "postgresql":
                    connection.exec_driver_sql(f"SET LOCAL statement_timeout = {timeout_ms}")
                try:
                    result = connection.execution_options(stream_results=True).execute(
                        text(safe_query), arguments
                    )
                    rows = result.mappings().fetchmany(max_rows)
                finally:
                    if raw_connection is not None:
                        raw_connection.set_progress_handler(None, 0)
            return ExecutionResult(status="SUCCEEDED", data=[dict(row) for row in rows])
        except Exception:
            if time.monotonic() >= deadline:
                return ExecutionResult(status="FAILED", error_code="AI_CONNECTOR_UNAVAILABLE", error_message="database query timed out")
            return ExecutionResult(status="FAILED", error_code="AI_CONNECTOR_UNAVAILABLE", error_message="database query failed")

    def _dialect(self) -> str:
        name = self.engine.dialect.name
        if name not in {"sqlite", "postgresql"}:
            raise ValueError("unsupported SQL dialect")
        return "postgres" if name == "postgresql" else name

    @staticmethod
    def _authorized_references(statement: exp.Select, allowed_tables: set[str], allowed_columns: set[str]) -> bool:
        tables = {table.name for table in statement.find_all(exp.Table)}
        columns = {column.name for column in statement.find_all(exp.Column)}
        # CTE 内的 DML 仍然可能产生写入；SELECT * 也不能绕过列级白名单。
        unsafe_nodes = {"Insert", "Update", "Delete", "Create", "Drop", "Alter", "Merge", "TruncateTable", "Command"}
        if any(node.__class__.__name__ in unsafe_nodes for node in statement.walk()):
            return False
        if any(True for _ in statement.find_all(exp.Star)):
            return False
        if statement.args.get("into") is not None or statement.args.get("locks"):
            return False
        return bool(tables) and tables.issubset(allowed_tables) and columns.issubset(allowed_columns)


class MdSkillScriptExecutor:
    tool_type = "MD_SCRIPT"

    def __init__(self) -> None:
        from app.config import get_settings
        settings = get_settings()
        windows_enabled = bool(getattr(settings, "md_skill_windows_process_enabled", False))
        self.runtime = MdSkillRuntime(
            settings.md_skills_dir, enabled=settings.md_skill_script_enabled,
            timeout_seconds=settings.md_skill_script_timeout, max_output_bytes=settings.md_skill_script_max_output,
            windows_process_enabled=windows_enabled,
            allowed_roots=getattr(settings, "resolved_sandbox_roots", None),
        )

    def execute(self, config: dict[str, Any], arguments: dict[str, Any], _timeout_seconds: float) -> ExecutionResult:
        return ExecutionResult(status="FAILED", error_code="AI_CAPABILITY_UNAVAILABLE", error_message="MD-Skill scripts require the controlled async runtime")
