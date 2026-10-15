import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import httpx
import yaml
from jsonschema import ValidationError, validate
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.kernel.audit.service import record_audit
from app.kernel.authz import PermissionChecker
from app.kernel.md_skill import MdSkillManifest
from app.kernel.md_skill_runtime import MdSkillRuntime
from app.kernel.skill_confirmation import pause_task_for_confirmation
from app.models import (
    Connector,
    Skill,
    SkillExecutionLog,
    SkillVersion,
    SkillVersionTool,
    TaskCheckpoint,
    Tool,
    ToolVersion,
)

_TOOL_TIMEOUT_MESSAGE = "tool execution timed out"
_TOOL_FAILURE_MESSAGE = "tool execution failed"
_PERMISSION_MESSAGE = "required permissions are missing"
_MCP_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    data: Any = None
    error_code: str | None = None
    error_message: str | None = None
    requires_confirmation: bool = False


class ToolExecutor(Protocol):
    tool_type: str

    def execute(self, config: dict[str, Any], arguments: dict[str, Any], timeout_seconds: float) -> ExecutionResult: ...


class AsyncToolExecutor(Protocol):
    async def execute_async(self, config: dict[str, Any], arguments: dict[str, Any]) -> ExecutionResult: ...


class MockExecutor:
    tool_type = "MOCK"

    def execute(self, config: dict[str, Any], arguments: dict[str, Any], _timeout_seconds: float) -> ExecutionResult:
        return ExecutionResult(status="SUCCEEDED", data={"echo": arguments, "config": config})


class McpExecutor:
    tool_type = "MCP"

    def execute(self, config: dict[str, Any], arguments: dict[str, Any], _timeout_seconds: float) -> ExecutionResult:
        return ExecutionResult(status="FAILED", error_code="AI_MCP_UNAVAILABLE", error_message="MCP connector is not connected and negotiated")


class MdSkillScriptExecutor:
    tool_type = "MD_SCRIPT"

    def __init__(self) -> None:
        from app.config import get_settings
        settings = get_settings()
        self.runtime = MdSkillRuntime(settings.md_skills_dir, enabled=settings.md_skill_script_enabled, timeout_seconds=settings.md_skill_script_timeout, max_output_bytes=settings.md_skill_script_max_output)

    def execute(self, config: dict[str, Any], arguments: dict[str, Any], _timeout_seconds: float) -> ExecutionResult:
        return ExecutionResult(status="FAILED", error_code="AI_CAPABILITY_UNAVAILABLE", error_message="MD-Skill scripts require the controlled async runtime")


class MdSkillInstructionLoader:
    """Load declarative SKILL.md content without executing embedded scripts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def load(self, relative_path: str) -> dict[str, Any]:
        path = (self.root / relative_path).resolve()
        if path.parent != self.root and self.root not in path.parents:
            raise ApiError("AI_SKILL_PATH_INVALID", "skill path escapes configured root", 422)
        if path.name != "SKILL.md" or not path.is_file():
            raise ApiError("AI_SKILL_NOT_FOUND", "SKILL.md not found", 404)
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise ApiError("AI_SKILL_METADATA_INVALID", "SKILL.md requires YAML front matter", 422)
        marker = text.find("\n---\n", 4)
        if marker < 0:
            raise ApiError("AI_SKILL_METADATA_INVALID", "SKILL.md front matter is not closed", 422)
        try:
            metadata = yaml.safe_load(text[4:marker]) or {}
        except yaml.YAMLError as exc:
            raise ApiError("AI_SKILL_METADATA_INVALID", "SKILL.md front matter is invalid", 422) from exc
        if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not isinstance(metadata.get("description"), str):
            raise ApiError("AI_SKILL_METADATA_INVALID", "SKILL.md requires name and description", 422)
        instructions = text[marker + 5:].strip()
        if not instructions:
            raise ApiError("AI_SKILL_CONTENT_INVALID", "SKILL.md instructions are empty", 422)
        return {"metadata": metadata, "instructions": instructions, "path": str(path)}


class McpConnector(Protocol):
    async def negotiate(self) -> None: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...
    async def close(self) -> None: ...


class NegotiatedMcpExecutor:
    tool_type = "MCP_NEGOTIATED"

    def __init__(self, connector: McpConnector) -> None:
        self.connector = connector
        self.negotiated = False

    async def connect(self) -> ExecutionResult:
        try:
            await self.connector.negotiate()
            self.negotiated = True
            return ExecutionResult(status="SUCCEEDED", data={"connected": True})
        except (OSError, ValueError, RuntimeError) as exc:
            self.negotiated = False
            return ExecutionResult(status="FAILED", error_code="AI_MCP_NEGOTIATION_FAILED", error_message=str(exc))

    async def execute_async(self, config: dict[str, Any], arguments: dict[str, Any]) -> ExecutionResult:
        if not self.negotiated:
            return ExecutionResult(status="FAILED", error_code="AI_MCP_UNAVAILABLE", error_message="MCP connector is not negotiated")
        try:
            result = await self.connector.call_tool(str(config["tool_name"]), arguments)
            return ExecutionResult(status="SUCCEEDED", data=result)
        except (OSError, ValueError, RuntimeError) as exc:
            return ExecutionResult(status="FAILED", error_code="AI_MCP_TOOL_CALL_FAILED", error_message=str(exc))


class StreamableHttpMcpConnector:
    protocol_version = "2025-06-18"

    def __init__(self, endpoint: str, *, allowed_hosts: list[str], timeout_seconds: float = 30, max_response_bytes: int = 1_000_000, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.endpoint = endpoint
        self.allowed_hosts = set(allowed_hosts)
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.session_id: str | None = None
        self._request_id = 0
        self.client = httpx.AsyncClient(follow_redirects=False, timeout=timeout_seconds, transport=transport)

    async def negotiate(self) -> None:
        result = await self._request("initialize", {"protocolVersion": self.protocol_version, "capabilities": {}, "clientInfo": {"name": "z-torque", "version": "0.1.0"}}, include_session=False)
        version = result.get("protocolVersion")
        if version != self.protocol_version:
            raise McpProtocolError(f"unsupported MCP protocol version: {version}")
        await self._notification("notifications/initialized")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._request("tools/call", {"name": name, "arguments": arguments})

    async def list_tools(self) -> Any:
        return await self._request("tools/list", {})

    async def close(self) -> None:
        if self.session_id:
            try:
                await self.client.delete(self.endpoint, headers={"Mcp-Session-Id": self.session_id})
            finally:
                self.session_id = None
        await self.client.aclose()

    async def _notification(self, method: str) -> None:
        await self._send({"jsonrpc": "2.0", "method": method}, expect_response=False)

    async def _request(self, method: str, params: dict[str, Any], include_session: bool = True) -> dict[str, Any]:
        self._request_id += 1
        response = await self._send({"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}, expect_response=True, include_session=include_session)
        if "error" in response:
            raise McpProtocolError(str(response["error"]))
        return response.get("result", {})

    async def _send(self, payload: dict[str, Any], *, expect_response: bool, include_session: bool = True) -> dict[str, Any]:
        from urllib.parse import urlparse
        parsed = urlparse(self.endpoint)
        # 回环地址允许 http（本地联调）；其余目标强制 https 且必须在 allowed_hosts 白名单内
        if parsed.hostname in _MCP_LOOPBACK_HOSTS:
            if parsed.scheme not in ("https", "http"):
                raise McpTransportError("MCP endpoint is not allowed")
        elif parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts:
            raise McpTransportError("MCP endpoint is not allowed")
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": self.protocol_version}
        if include_session and self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            response = await self.client.post(self.endpoint, headers=headers, json=payload)
            if response.status_code >= 400:
                raise McpTransportError(f"MCP HTTP status {response.status_code}")
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id:
                self.session_id = session_id
            if not expect_response:
                return {}
            if len(response.content) > self.max_response_bytes:
                raise McpTransportError("MCP response exceeds configured limit")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type == "application/json":
                return response.json()
            if content_type == "text/event-stream":
                return self._parse_sse(response.text)
            raise McpTransportError("unsupported MCP response content type")
        except asyncio.CancelledError:
            raise
        except (httpx.TimeoutException, httpx.HTTPError, json.JSONDecodeError) as exc:
            raise McpTransportError(str(exc)) from exc

    @staticmethod
    def _parse_sse(body: str) -> dict[str, Any]:
        for block in body.split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
            if data:
                return json.loads(data)
        raise McpTransportError("MCP SSE response did not contain JSON-RPC data")


class McpTransportError(Exception):
    pass


class McpProtocolError(Exception):
    pass


class SkillGateway:
    def __init__(self, permission_checker: PermissionChecker | None = None) -> None:
        self.permission_checker = permission_checker or PermissionChecker()
        self._executors: dict[str, ToolExecutor] = {}
        from app.config import get_settings
        settings = get_settings()
        self._md_runtime = MdSkillRuntime(settings.md_skills_dir, enabled=settings.md_skill_script_enabled, timeout_seconds=settings.md_skill_script_timeout, max_output_bytes=settings.md_skill_script_max_output)

    def register(self, executor: ToolExecutor) -> None:
        self._executors[executor.tool_type] = executor

    async def _execute_mcp(self, config: dict[str, Any], arguments: dict[str, Any], timeout_seconds: float) -> ExecutionResult:
        from app.config import get_settings

        transport = str(config.get("transport", "http")).lower()
        connector: McpConnector
        if transport == "http":
            endpoint = config.get("endpoint")
            if not isinstance(endpoint, str):
                return ExecutionResult(status="FAILED", error_code="AI_MCP_CONFIG_INVALID", error_message="MCP endpoint is required")
            settings = get_settings()
            connector = StreamableHttpMcpConnector(endpoint, allowed_hosts=settings.mcp_allowed_hosts, timeout_seconds=timeout_seconds, max_response_bytes=settings.mcp_max_response_bytes)
        elif transport == "stdio":
            registry = getattr(self, "stdio_registry", None)
            server_name = config.get("server_name")
            if registry is None or not isinstance(server_name, str):
                return ExecutionResult(status="FAILED", error_code="AI_CONNECTOR_UNAVAILABLE", error_message="MCP stdio server is not registered")
            connector = registry.connector(server_name, timeout_seconds=timeout_seconds)
        else:
            return ExecutionResult(status="FAILED", error_code="AI_REQUEST_INVALID", error_message="unsupported MCP transport")
        executor = NegotiatedMcpExecutor(connector)
        try:
            connected = await executor.connect()
            if connected.status != "SUCCEEDED":
                return connected
            return await asyncio.wait_for(executor.execute_async(config, arguments), timeout=timeout_seconds)
        except TimeoutError:
            return ExecutionResult(status="FAILED", error_code="AI_SKILL_TIMEOUT", error_message=_TOOL_TIMEOUT_MESSAGE)
        except (OSError, ValueError, RuntimeError):
            return ExecutionResult(status="FAILED", error_code="AI_SKILL_FAILED", error_message="MCP tool execution failed")
        finally:
            await connector.close()

    async def _execute_with_retry(self, executor: ToolExecutor, config: dict[str, Any], arguments: dict[str, Any], *, timeout_seconds: float, max_retries: int, backoff_seconds: float) -> ExecutionResult:
        result = ExecutionResult(status="FAILED", error_code="AI_SKILL_FAILED", error_message="tool execution failed")
        for attempt in range(max_retries + 1):
            try:
                if hasattr(executor, "execute_async"):
                    result = await asyncio.wait_for(executor.execute_async(config, arguments), timeout=timeout_seconds)
                else:
                    result = await asyncio.wait_for(asyncio.to_thread(executor.execute, config, arguments, timeout_seconds), timeout=timeout_seconds)
            except TimeoutError:
                result = ExecutionResult(status="FAILED", error_code="AI_SKILL_TIMEOUT", error_message=_TOOL_TIMEOUT_MESSAGE)
            except (OSError, ValueError, RuntimeError):
                result = ExecutionResult(status="FAILED", error_code="AI_SKILL_FAILED", error_message=_TOOL_FAILURE_MESSAGE)
            if result.status == "SUCCEEDED" or attempt == max_retries or not self._is_retryable(result):
                return result
            await asyncio.sleep(min(backoff_seconds * (2 ** attempt), 30))
        return result

    @staticmethod
    def _is_retryable(result: ExecutionResult) -> bool:
        return result.error_code in {
            "AI_SKILL_TIMEOUT",
            "AI_SKILL_FAILED",
            "AI_HTTP_REQUEST_FAILED",
            "AI_HTTP_ERROR",
            "AI_MCP_TOOL_CALL_FAILED",
            "AI_MCP_NEGOTIATION_FAILED",
        }

    def execute(
        self,
        db: Session,
        *,
        tenant_id: UUID,
        user_id: UUID | None,
        skill_version_id: UUID,
        arguments: dict[str, Any],
        task_id: UUID | None = None,
        timeout_seconds: float = 30,
    ) -> ExecutionResult:
        skill = db.get(SkillVersion, skill_version_id)
        if not skill or skill.tenant_id != tenant_id or skill.status != "PUBLISHED":
            raise ApiError("AI_SKILL_NOT_FOUND", "published skill version not found", 404)
        # 父级 Skill 已归档时版本不可再执行
        parent_skill = db.get(Skill, skill.skill_id)
        if parent_skill is None or parent_skill.status != "ACTIVE":
            raise ApiError("AI_SKILL_NOT_FOUND", "skill is unavailable", 404)
        if not self.permission_checker.has_all(db, user_id, skill.required_permissions):
            raise ApiError("AI_PERMISSION_DENIED", _PERMISSION_MESSAGE, 403)
        links = db.query(SkillVersionTool).filter(SkillVersionTool.skill_version_id == skill.id).all()
        if not links:
            result = ExecutionResult(status="SUCCEEDED", data={"instructions": skill.instructions})
            self._log(db, tenant_id, user_id, skill, None, task_id, result)
            return result
        results = []
        for link in links:
            tool = db.get(ToolVersion, link.tool_version_id)
            if not tool or tool.tenant_id != tenant_id or tool.status != "PUBLISHED":
                raise ApiError("AI_TOOL_NOT_FOUND", "published tool version not found", 404)
            connector = None
            connector_id = tool.config_snapshot.get("connector_id")
            if connector_id:
                connector = db.get(Connector, connector_id)
                if not connector or connector.tenant_id != tenant_id or connector.status != "ENABLED":
                    raise ApiError("AI_CONNECTOR_UNAVAILABLE", "connector is unavailable", 503)
            if not self.permission_checker.has_all(db, user_id, tool.required_permissions):
                raise ApiError("AI_PERMISSION_DENIED", "required permissions are missing", 403)
            self._validate_input(tool.input_schema, arguments)
            if task_id and user_id and (skill.requires_confirmation or tool.requires_confirmation):
                pending_call = {"tool_version_id": str(tool.id), "arguments": arguments}
                confirmation = pause_task_for_confirmation(
                    db,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    task_id=task_id,
                    reason="skill or tool requires human confirmation",
                    snapshot={"skill_version_id": str(skill.id), "tool_version_id": str(tool.id), "arguments": arguments},
                    execution_context={"skill_version_id": str(skill.id), "tool_version_id": str(tool.id)},
                    completed_steps=[],
                    pending_steps=[str(tool.id)],
                    pending_call=pending_call,
                    idempotency_key=f"{task_id}:{tool.id}",
                    skill_version_id=skill.id,
                    tool_version_id=tool.id,
                )
                result = ExecutionResult(status="WAITING_HUMAN", requires_confirmation=True, data={"confirmation_id": str(confirmation.id)})
                self._log(db, tenant_id, user_id, skill, tool, task_id, result, arguments=arguments)
                return result
            owner = db.get(Tool, tool.tool_id)
            # 父级 Tool 已归档时版本不可再执行
            if owner is None or owner.status != "ACTIVE":
                result = ExecutionResult(status="FAILED", error_code="AI_TOOL_NOT_FOUND", error_message="published tool version not found")
                results.append(result.data)
                self._log(db, tenant_id, user_id, skill, tool, task_id, result, arguments=arguments)
                return result
            executor = self._executors.get(owner.tool_type) if owner else None
            if owner and owner.tool_type == "MCP":
                result = ExecutionResult(status="FAILED", error_code="AI_MCP_ASYNC_REQUIRED", error_message="MCP execution requires execute_async")
                results.append(result.data)
                self._log(db, tenant_id, user_id, skill, tool, task_id, result, arguments=arguments)
                if result.status != "SUCCEEDED":
                    return result
                continue
            if executor is None:
                result = ExecutionResult(status="FAILED", error_code="AI_EXECUTOR_UNAVAILABLE", error_message="executor is unavailable")
            else:
                try:
                    result = executor.execute(self._materialize_config(tool, connector), arguments, timeout_seconds)
                except TimeoutError:
                    result = ExecutionResult(status="FAILED", error_code="AI_SKILL_TIMEOUT", error_message="tool execution timed out")
                except (OSError, ValueError, RuntimeError):
                    result = ExecutionResult(status="FAILED", error_code="AI_SKILL_FAILED", error_message="tool execution failed")
            result = self._validate_output(tool.output_schema, result)
            results.append(result.data)
            self._log(db, tenant_id, user_id, skill, tool, task_id, result, arguments=arguments)
            if result.status != "SUCCEEDED":
                return result
        return ExecutionResult(status="SUCCEEDED", data=results)

    async def execute_async(self, db: Session, **kwargs: Any) -> ExecutionResult:
        """Execute sync and negotiated async tools through one audited gateway path."""
        skill_version_id = kwargs.pop("skill_version_id")
        tenant_id = kwargs.pop("tenant_id")
        user_id = kwargs.pop("user_id", None)
        arguments = kwargs.pop("arguments")
        task_id = kwargs.pop("task_id", None)
        timeout_seconds = kwargs.pop("timeout_seconds", 30)
        resume_checkpoint_id = kwargs.pop("resume_checkpoint_id", None)
        cancellation_event = kwargs.pop("cancellation_event", None)
        deadline_at = kwargs.pop("deadline_at", None)
        # Agent 工具调用循环传入 tool_version_id 时，仅执行模型选择的那个工具；
        # 不传则保持原语义（按绑定顺序执行全部工具）。
        tool_version_id = kwargs.pop("tool_version_id", None)
        if kwargs:
            raise TypeError(f"unexpected execution arguments: {', '.join(kwargs)}")
        if self._cancelled(cancellation_event):
            return ExecutionResult(status="CANCELLED", error_code="AI_TASK_NOT_CANCELABLE", error_message="task cancellation requested")
        if resume_checkpoint_id is not None:
            checkpoint = db.get(TaskCheckpoint, resume_checkpoint_id)
            if checkpoint is None or checkpoint.task_id != task_id or checkpoint.status not in {"APPROVED", "RESUMED"}:
                raise ApiError("AI_CHECKPOINT_INVALID", "checkpoint is not valid for this task", 409)
        skill = db.get(SkillVersion, skill_version_id)
        if not skill or skill.tenant_id != tenant_id or skill.status != "PUBLISHED":
            raise ApiError("AI_SKILL_NOT_FOUND", "published skill version not found", 404)
        # 父级 Skill 已归档时版本不可再执行
        parent_skill = db.get(Skill, skill.skill_id)
        if parent_skill is None or parent_skill.status != "ACTIVE":
            raise ApiError("AI_SKILL_NOT_FOUND", "skill is unavailable", 404)
        if not self.permission_checker.has_all(db, user_id, skill.required_permissions):
            raise ApiError("AI_PERMISSION_DENIED", "required permissions are missing", 403)
        links = db.query(SkillVersionTool).filter(SkillVersionTool.skill_version_id == skill.id).all()
        if tool_version_id is not None:
            wanted = str(tool_version_id)
            links = [link for link in links if str(link.tool_version_id) == wanted]
            if not links:
                raise ApiError("AI_TOOL_NOT_FOUND", "tool version is not bound to this skill version", 404)
        results = []
        if not links and skill.config_snapshot.get("implementation") == "md_script":
            manifest = MdSkillManifest(
                name=skill.config_snapshot["name"], description=skill.config_snapshot["description"],
                implementation=skill.config_snapshot["implementation"], declared_version=skill.config_snapshot["declared_version"],
                input_schema=skill.config_snapshot["input_schema"], output_schema=skill.config_snapshot["output_schema"],
                allow_scripts=skill.config_snapshot["allow_scripts"], required_permissions=skill.config_snapshot["required_permissions"],
                instructions=skill.instructions or "", script_files=skill.config_snapshot["script_files"],
                file_fingerprint=skill.config_snapshot["file_fingerprint"], relative_path=skill.config_snapshot["relative_path"],
            )
            try:
                data = await self._md_runtime.execute(manifest, arguments)
                result = ExecutionResult(status="SUCCEEDED", data=data)
            except ApiError as exc:
                result = ExecutionResult(status="FAILED", error_code=exc.code, error_message=exc.message)
            self._log(db, tenant_id, user_id, skill, None, task_id, result)
            return result
        for link in links:
            if self._cancelled(cancellation_event):
                return ExecutionResult(status="CANCELLED", error_code="AI_TASK_NOT_CANCELABLE", error_message="task cancellation requested")
            tool = db.get(ToolVersion, link.tool_version_id)
            owner = db.get(Tool, tool.tool_id) if tool else None
            # 父级 Tool 已归档时版本不可再执行
            if not tool or not owner or owner.status != "ACTIVE" or tool.tenant_id != tenant_id or tool.status != "PUBLISHED":
                raise ApiError("AI_TOOL_NOT_FOUND", "published tool version not found", 404)
            connector = None
            connector_id = tool.config_snapshot.get("connector_id")
            if connector_id:
                connector = db.get(Connector, connector_id)
                if not connector or connector.tenant_id != tenant_id or connector.status != "ENABLED":
                    raise ApiError("AI_CONNECTOR_UNAVAILABLE", "connector is unavailable", 503)
            if not self.permission_checker.has_all(db, user_id, tool.required_permissions):
                raise ApiError("AI_PERMISSION_DENIED", "required permissions are missing", 403)
            self._validate_input(tool.input_schema, arguments)
            if task_id and user_id and not resume_checkpoint_id and (skill.requires_confirmation or tool.requires_confirmation):
                pending_call = {"tool_version_id": str(tool.id), "arguments": arguments}
                confirmation = pause_task_for_confirmation(
                    db,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    task_id=task_id,
                    reason="skill or tool requires human confirmation",
                    snapshot={"skill_version_id": str(skill.id), "tool_version_id": str(tool.id), "arguments": arguments},
                    execution_context={"skill_version_id": str(skill.id), "tool_version_id": str(tool.id)},
                    completed_steps=[],
                    pending_steps=[str(tool.id)],
                    pending_call=pending_call,
                    idempotency_key=f"{task_id}:{tool.id}",
                    skill_version_id=skill.id,
                    tool_version_id=tool.id,
                )
                result = ExecutionResult(status="WAITING_HUMAN", requires_confirmation=True, data={"confirmation_id": str(confirmation.id)})
                self._log(db, tenant_id, user_id, skill, tool, task_id, result, arguments=arguments)
                return result
            executor = self._executors.get(owner.tool_type)
            if owner.tool_type == "MCP":
                result = await self._execute_mcp(tool.config_snapshot, arguments, self._remaining_timeout(timeout_seconds, deadline_at))
                self._log(db, tenant_id, user_id, skill, tool, task_id, result, arguments=arguments)
                if result.status != "SUCCEEDED":
                    return result
                results.append(result.data)
                continue
            if executor is None:
                result = ExecutionResult(status="FAILED", error_code="AI_EXECUTOR_UNAVAILABLE", error_message="executor is unavailable")
            else:
                result = await self._execute_with_retry(executor, self._materialize_config(tool, connector), arguments, timeout_seconds=min(self._remaining_timeout(timeout_seconds, deadline_at), tool.timeout_seconds), max_retries=tool.max_retries, backoff_seconds=tool.retry_backoff_seconds)
            result = self._validate_output(tool.output_schema, result)
            self._log(db, tenant_id, user_id, skill, tool, task_id, result, arguments=arguments)
            if result.status != "SUCCEEDED":
                return result
            results.append(result.data)
        return ExecutionResult(status="SUCCEEDED", data=results or {"instructions": skill.instructions})

    @staticmethod
    def _cancelled(event: asyncio.Event | None) -> bool:
        return event is not None and event.is_set()

    @staticmethod
    def _remaining_timeout(timeout_seconds: float, deadline_at: Any) -> float:
        if deadline_at is None:
            return timeout_seconds
        from datetime import UTC, datetime
        remaining = (deadline_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise ApiError("AI_TASK_TIMEOUT", "task deadline exceeded", 504)
        return min(timeout_seconds, remaining)

    def _materialize_config(self, tool: ToolVersion, connector: Connector | None) -> dict[str, Any]:
        """构造运行时配置：解密连接器 Secret URI 供 {{secret}} 模板使用，并兜底 allowed_hosts。"""
        config = dict(tool.config_snapshot or {})
        if connector is not None and connector.connector_type == "DB":
            # DB 工具：连接信息以连接器当前 config 为准（运行时叠加，避免注册快照过期）；
            # allowed_tables 取当前连接器白名单，执行器会与注册快照求交集（fail-closed）。
            for key in ("driver", "host", "port", "database", "username"):
                if key in (connector.config or {}):
                    config[key] = connector.config[key]
            tables = (connector.config or {}).get("allowed_tables")
            if isinstance(tables, list):
                config["connector_allowed_tables"] = [str(table) for table in tables]
        if connector is not None and connector.secret_ref:
            from app.security.secret_store import SecretStore, SecretStoreError

            try:
                config["secret_value"] = SecretStore().decrypt(connector.secret_ref.removeprefix("secret://"))
            except SecretStoreError as exc:
                raise ApiError("AI_CONNECTOR_SECRET_INVALID", "connector secret cannot be decrypted", 503) from exc
        url = str(config.get("url", ""))
        if url:
            from urllib.parse import urlparse

            hosts = {str(item) for item in (config.get("allowed_hosts") or [])}
            parsed = urlparse(url)
            if parsed.hostname:
                hosts.add(parsed.hostname)
            base_url = (connector.config or {}).get("base_url") if connector is not None else None
            base_host = urlparse(str(base_url)).hostname if base_url else None
            if base_host:
                hosts.add(base_host)
            config["allowed_hosts"] = sorted(hosts)
        return config

    @staticmethod
    def _validate_input(schema: dict[str, Any] | None, arguments: dict[str, Any]) -> None:
        if not schema:
            return
        try:
            validate(arguments, schema)
        except ValidationError as exc:
            raise ApiError("AI_PARAM_INVALID", "tool input does not match its schema", 400, {"path": list(exc.path)}) from exc

    @staticmethod
    def _validate_output(schema: dict[str, Any] | None, result: ExecutionResult) -> ExecutionResult:
        if result.status != "SUCCEEDED" or not schema:
            return result
        try:
            validate(result.data, schema)
        except ValidationError:
            return ExecutionResult(status="FAILED", error_code="AI_TASK_RESULT_INVALID", error_message="tool output does not match its schema")
        return result

    @staticmethod
    def _log(db: Session, tenant_id: UUID, user_id: UUID | None, skill: SkillVersion, tool: ToolVersion | None, task_id: UUID | None, result: ExecutionResult, arguments: dict | None = None) -> None:
        db.add(SkillExecutionLog(tenant_id=tenant_id, actor_user_id=user_id, skill_version_id=skill.id, tool_version_id=tool.id if tool else None, task_id=task_id, status=result.status, result={**({"data": result.data} if result.data is not None else {}), **({"input": arguments} if arguments else {})}, error_code=result.error_code))
        record_audit(db, tenant_id=tenant_id, actor_user_id=user_id, action="SKILL_EXECUTED", resource_type="SKILL_VERSION", resource_id=skill.id, summary="skill execution completed", detail={"status": result.status, "tool_version_id": str(tool.id) if tool else None})
        db.flush()
