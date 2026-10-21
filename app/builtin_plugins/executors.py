"""内置工具执行器插件（HTTP / DB_QUERY / MOCK / MCP 占位 / MD_SCRIPT）。

自 app/kernel/builtin_executors.py 与 app/kernel/skill_gateway.py 迁移，逻辑不变；
执行器契约见 app/kernel/ports/skill_executor.py。
DB_SQL（连接器目标库执行）仍在 app/kernel/db_executor.py（含注册期 SQL 校验纯函数），
由装配根直接引用。
"""

import ipaddress
import json
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpcore
import httpx
from sqlalchemy import Engine, text
from sqlglot import exp, parse

from app.kernel.ports.skill_executor import ExecutionResult
from app.kernel.md_skill_runtime import MdSkillRuntime

# HTTP 工具方法白名单：注册入口（OpenAPI 导入 / 手动注册）与执行器引用同一来源，
# 将来放开新方法时改这里即可，两个闸门不会漂移
SUPPORTED_HTTP_METHODS = ("GET", "POST")

_TEMPLATE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")


class MockExecutor:
    tool_type = "MOCK"

    def execute(self, config: dict[str, Any], arguments: dict[str, Any], _timeout_seconds: float) -> ExecutionResult:
        return ExecutionResult(status="SUCCEEDED", data={"echo": arguments, "config": config})


class McpExecutor:
    tool_type = "MCP"

    def execute(self, config: dict[str, Any], arguments: dict[str, Any], _timeout_seconds: float) -> ExecutionResult:
        return ExecutionResult(status="FAILED", error_code="AI_MCP_UNAVAILABLE", error_message="MCP connector is not connected and negotiated")


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
        try:
            client = self._client(timeout_seconds, host, pinned_ip)
        except RuntimeError:
            # 无法把连接固定到已校验地址时 fail-closed，避免退化为可被 rebinding 绕过的校验
            return ExecutionResult(status="FAILED", error_code="AI_HTTP_TARGET_BLOCKED", error_message="HTTP client cannot pin the validated address")
        try:
            with client:
                if method == "POST":
                    # 导入工具约定：requestBody 整体作为 __body__ 参数；其余参数拼 query
                    body = arguments.get("__body__")
                    query_args = {key: value for key, value in arguments.items() if key != "__body__"}
                    response = client.post(url, json=body, params={**query_params, **query_args}, headers=headers)
                else:
                    params: dict[str, Any] = {**query_params, **arguments}
                    response = client.get(url, params=params, headers=headers)
            body = response.text[: int(config.get("max_response_bytes", 1_000_000))]
            # body 为响应原文（截断）；能解析为 JSON 时附加 json 字段，
            # 组合工具步骤间即可引用内层字段（$steps.<key>.output.json.<path>）
            data: dict[str, Any] = {"status_code": response.status_code, "body": body}
            try:
                data["json"] = json.loads(body)
            except ValueError:
                data["json"] = None
            return ExecutionResult(status="SUCCEEDED" if response.is_success else "FAILED", data=data, error_code=None if response.is_success else "AI_HTTP_ERROR")
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
    def _client(timeout_seconds: float, host: str, pinned_ip: str | None) -> httpx.Client:
        """构造 HTTP 客户端；公网目标把连接钉在已校验 IP 上（pinned_ip=None 表示显式放行内网）。"""
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
        dialect = self._dialect()
        try:
            statements = parse(query, read=dialect)
        except Exception:
            return ExecutionResult(status="FAILED", error_code="AI_REQUEST_INVALID", error_message="query parse failed")
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            return ExecutionResult(status="FAILED", error_code="AI_REQUEST_INVALID", error_message="exactly one SELECT statement is required")
        allowed_tables = set(config.get("allowed_tables", []))
        allowed_columns = set(config.get("allowed_columns", []))
        if not self._authorized_references(statements[0], allowed_tables, allowed_columns):
            return ExecutionResult(status="FAILED", error_code="AI_FORBIDDEN", error_message="query references an unauthorized table or column")
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(text(query), arguments).mappings().fetchmany(int(config.get("max_rows", 100)))
            return ExecutionResult(status="SUCCEEDED", data=[dict(row) for row in rows])
        except Exception:
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
        return bool(tables) and tables.issubset(allowed_tables) and columns.issubset(allowed_columns)


class MdSkillScriptExecutor:
    tool_type = "MD_SCRIPT"

    def __init__(self) -> None:
        from app.config import get_settings
        settings = get_settings()
        self.runtime = MdSkillRuntime(settings.md_skills_dir, enabled=settings.md_skill_script_enabled, timeout_seconds=settings.md_skill_script_timeout, max_output_bytes=settings.md_skill_script_max_output)

    def execute(self, config: dict[str, Any], arguments: dict[str, Any], _timeout_seconds: float) -> ExecutionResult:
        return ExecutionResult(status="FAILED", error_code="AI_CAPABILITY_UNAVAILABLE", error_message="MD-Skill scripts require the controlled async runtime")
