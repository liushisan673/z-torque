import asyncio
import logging
from typing import Any
from uuid import UUID

from jsonschema import ValidationError, validate
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.core.redaction import redact_mapping
from app.kernel.audit.service import AuditExecutionInterceptor, record_audit_detached
from app.kernel.authz import PermissionChecker
from app.kernel.md_skill import MdSkillManifest
from app.kernel.md_skill_runtime import MdSkillRuntime
from app.kernel.plugin_guard import CIRCUIT_OPEN_CODE, QUOTA_EXCEEDED_CODE, PluginGuardRegistry
from app.kernel.ports.approval_provider import ApprovalProvider
from app.kernel.ports.authz_provider import AuthzProvider
from app.kernel.ports.connector import McpConnector, McpProtocolError, McpTransportError
from app.kernel.ports.interceptor import ExecutionInterceptor
from app.kernel.ports.sandbox_driver import SandboxDriver
from app.kernel.ports.secret_provider import SecretProvider
from app.kernel.ports.skill_executor import ExecutionResult, ToolExecutor
from app.kernel.registry.active import try_port
from app.kernel.security_context import SecurityContext
from app.kernel.skill_confirmation import BuiltinApprovalProvider, pause_task_for_confirmation
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
from app.services.tool_mapping_builder import _sanitize_schema

_TOOL_TIMEOUT_MESSAGE = "tool execution timed out"
_TOOL_FAILURE_MESSAGE = "tool execution failed"
_PERMISSION_MESSAGE = "required permissions are missing"

# 沙箱相关错误码 → 审计 action（v1.3 §4.6）：只有沙箱语义的失败才计入"沙箱"类别
_SANDBOX_ACTION_BY_CODE = {
    "AI_SANDBOX_DENIED": "SANDBOX_DENIED",
    "AI_SANDBOX_RESOURCE_EXCEEDED": "SANDBOX_RESOURCE_EXCEEDED",
    "AI_SKILL_TIMEOUT": "SANDBOX_TIMEOUT",
}

# 执行器/连接器可能抛出的运行期异常：统一映射为失败结果而非穿透为 500，
# 否则该次调用不会写入 SkillExecutionLog（契约红线1：每次执行必须记账）。
_EXECUTION_ERRORS = (OSError, ValueError, RuntimeError, McpTransportError, McpProtocolError)

logger = logging.getLogger("z_torque.skill_gateway")


def _path_get(data: Any, path: str) -> Any:
    """按 a.b[0].c 形式取值；缺键抛 KeyError，越界抛 IndexError。"""
    current = data
    for part in path.replace("[", ".").replace("]", "").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(part)
            current = current[part]
        elif isinstance(current, (list, tuple)):
            current = current[int(part)]
        else:
            raise KeyError(part)
    return current


def _resolve_composite_value(value: Any, inputs: dict[str, Any], step_outputs: dict[str, Any]) -> Any:
    if isinstance(value, str):
        if value.startswith("$input."):
            return _path_get(inputs, value[len("$input."):])
        if value.startswith("$steps."):
            expr = value[len("$steps."):]
            key, sep, rest = expr.partition(".output")
            if not sep or key not in step_outputs:
                raise KeyError(key)
            return _path_get(step_outputs[key], rest.lstrip("."))
        return value
    if isinstance(value, dict):
        return {k: _resolve_composite_value(v, inputs, step_outputs) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_composite_value(item, inputs, step_outputs) for item in value]
    return value


def _resolve_composite_arguments(arguments: Any, inputs: dict[str, Any], step_outputs: dict[str, Any]) -> dict[str, Any]:
    resolved = _resolve_composite_value(arguments or {}, inputs, step_outputs)
    return resolved if isinstance(resolved, dict) else {}


def _resolve_step_arguments(raw_arguments: Any, inputs: dict[str, Any], step_outputs: dict[str, Any], child_input_schema: Any) -> dict[str, Any]:
    """解析单步入参：配置了模板按模板解析；留空则透传本次调用入参
    （子工具 schema 声明了属性时按其过滤，未声明则原样透传）。
    """
    if raw_arguments:
        return _resolve_composite_arguments(raw_arguments, inputs, step_outputs)
    props = (child_input_schema or {}).get("properties")
    if isinstance(props, dict) and props:
        return {name: value for name, value in inputs.items() if name in props}
    return dict(inputs)


def _select_composite_output(output_path: str, step_outputs: dict[str, Any]) -> Any:
    if not output_path.startswith("$steps."):
        raise ValueError("output path must start with $steps.")
    expr = output_path[len("$steps."):]
    key, sep, rest = expr.partition(".output")
    if not sep or key not in step_outputs:
        raise KeyError(key)
    return _path_get(step_outputs[key], rest.lstrip("."))


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
        except _EXECUTION_ERRORS as exc:
            self.negotiated = False
            return ExecutionResult(status="FAILED", error_code="AI_MCP_NEGOTIATION_FAILED", error_message=str(exc))
        except Exception as exc:  # noqa: BLE001 - 传输实现可插拔，不能让异常穿透为 500
            logger.exception("unexpected MCP negotiation failure")
            self.negotiated = False
            return ExecutionResult(status="FAILED", error_code="AI_MCP_NEGOTIATION_FAILED", error_message=str(exc) or "MCP negotiation failed")

    async def execute_async(self, config: dict[str, Any], arguments: dict[str, Any]) -> ExecutionResult:
        if not self.negotiated:
            return ExecutionResult(status="FAILED", error_code="AI_MCP_UNAVAILABLE", error_message="MCP connector is not negotiated")
        try:
            result = await self.connector.call_tool(str(config["tool_name"]), arguments)
            return ExecutionResult(status="SUCCEEDED", data=result)
        except _EXECUTION_ERRORS as exc:
            return ExecutionResult(status="FAILED", error_code="AI_MCP_TOOL_CALL_FAILED", error_message=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("unexpected MCP tool call failure")
            return ExecutionResult(status="FAILED", error_code="AI_MCP_TOOL_CALL_FAILED", error_message=str(exc) or "MCP tool call failed")


class SkillGateway:
    def __init__(self, permission_checker: AuthzProvider | None = None, *, http_connector_factory: Any = None, stdio_registry: Any = None, approval_provider: ApprovalProvider | None = None, sandbox_driver: SandboxDriver | None = None, interceptor: ExecutionInterceptor | None = None, plugin_guard: PluginGuardRegistry | None = None, secret_provider: SecretProvider | None = None) -> None:
        # authz.provider Port 解析结果由装配根经插件注入（P1-3）；默认回退保留内置 RBAC
        # 检查器，供无装配的直构场景（测试/脚本）使用，语义与插件包装实现一致
        self.permission_checker = permission_checker or PermissionChecker()
        # approval / sandbox / interceptor 三 Port 解析结果由装配根经插件注入（P2 收口）；
        # 未注入时回退内核默认实现（直构场景语义不变，单一路径无分支）
        self._approvals = approval_provider or BuiltinApprovalProvider()
        self._interceptor = interceptor or AuditExecutionInterceptor()
        # 插件级熔断与配额（契约 §5.5）：执行器分派闸门（按 tool_type 键）；
        # 未注入时按全局配置默认构建
        self._guard = plugin_guard or PluginGuardRegistry.from_settings()
        self._executors: dict[str, ToolExecutor] = {}
        # 连接器传输由 connector Port 插件注入（内核不感知实现，阶段00 §3.8 约束 1）
        self.http_connector_factory = http_connector_factory
        self.stdio_registry = stdio_registry
        # 密钥解密经 secret.provider Port（契约 §2 红线3：内核不得直连具体存储）；
        # 未注入时回退内置 Fernet 存储，供直构场景（测试/脚本）使用
        self._secrets = secret_provider
        from app.config import get_settings
        settings = get_settings()
        self._md_runtime = MdSkillRuntime(settings.md_skills_dir, enabled=settings.md_skill_script_enabled, timeout_seconds=settings.md_skill_script_timeout, max_output_bytes=settings.md_skill_script_max_output, sandbox_driver=sandbox_driver)

    def register(self, executor: ToolExecutor) -> None:
        self._executors[executor.tool_type] = executor

    @staticmethod
    def _audit_permission_denied(*, ctx: SecurityContext, resource_type: str, resource_id: UUID | None) -> None:
        """拒绝必须留痕：独立事务写入，避免随业务回滚丢失（P2-12）。"""
        record_audit_detached(
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            action="AUTHORIZATION_DENIED",
            resource_type=resource_type,
            resource_id=resource_id,
            summary=_PERMISSION_MESSAGE,
            detail={"error_code": "AI_PERMISSION_DENIED"},
            trace_id=ctx.trace_id,
        )

    @staticmethod
    def _audit_sandbox_event(*, ctx: SecurityContext, skill_version_id: UUID | None, code: str, message: str) -> None:
        """沙箱拒绝/超限留痕（v1.3 §4.2 / 决策 C：审计由网关写，插件零审计）。

        沙箱驱动本身无 SecurityContext（run 签名锁定），拒绝事件由网关按
        error_code 判定后以独立事务落库，随业务回滚也不会丢失。
        """
        action = _SANDBOX_ACTION_BY_CODE.get(code)
        if action is None:
            return
        record_audit_detached(
            tenant_id=ctx.tenant_id,
            actor_user_id=ctx.user_id,
            action=action,
            resource_type="MD_SKILL_SCRIPT",
            resource_id=skill_version_id,
            summary=message or code,
            detail={"error_code": code},
            trace_id=ctx.trace_id,
        )

    def _decrypt_secret(self, ciphertext: str) -> str:
        """经注入的 secret.provider 解密；未注入时回退内置存储（直构场景）。"""
        if self._secrets is not None:
            return self._secrets.decrypt(ciphertext)
        from app.security.secret_store import SecretStore

        return SecretStore().decrypt(ciphertext)

    def _resolve_secret_token(self, secret_ref: str | None) -> str | None | ExecutionResult:
        """解密 Secret URI 为连接器凭证；失败返回 FAILED 结果（含 503 语义错误码）。"""
        if not secret_ref:
            return None
        try:
            return self._decrypt_secret(secret_ref.removeprefix("secret://"))
        except Exception:  # noqa: BLE001 - 解密实现可替换（KMS 等），统一降级为凭证无效
            logger.warning("connector secret decryption failed", exc_info=True)
            return ExecutionResult(status="FAILED", error_code="AI_CONNECTOR_SECRET_INVALID", error_message="连接器凭证无法解密")

    async def probe_mcp_http(self, endpoint: str, *, allowed_hosts: list[str], timeout_seconds: float, max_response_bytes: int, secret_ref: str | None = None, allow_private_network: bool = False, proxy: str | None = None) -> dict:
        """MCP HTTP 探测：initialize 握手 + tools/list，返回 tools/list 原始结果。

        供 API 层连接器测试/工具发现使用；传输实现经装配注入的 http_connector_factory
        构造，内核不感知插件类型（阶段00 §3.8 约束 1，G1 修复）。
        失败抛 McpProtocolError / McpTransportError / ApiError，由调用方映射错误码。
        """
        if self.http_connector_factory is None:
            raise ApiError("AI_CONNECTOR_UNAVAILABLE", "MCP HTTP connector is unavailable", 503)
        auth_token = self._resolve_secret_token(secret_ref)
        if isinstance(auth_token, ExecutionResult):
            raise ApiError(auth_token.error_code or "AI_CONNECTOR_SECRET_INVALID", auth_token.error_message or "连接器凭证无法解密", 503)
        connector = self.http_connector_factory(endpoint, allowed_hosts=allowed_hosts, timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes, auth_token=auth_token, allow_private_network=allow_private_network, proxy=proxy)
        try:
            await connector.negotiate()
            return await connector.list_tools()
        finally:
            # close 失败不得覆盖握手/发现结果或原始异常
            try:
                await connector.close()
            except Exception:  # noqa: BLE001
                logger.warning("failed to close MCP connector after probe", exc_info=True)

    async def _execute_mcp(self, config: dict[str, Any], arguments: dict[str, Any], timeout_seconds: float, *, secret_ref: str | None = None, identity: dict[str, str] | None = None) -> ExecutionResult:
        from app.config import get_settings

        transport = str(config.get("transport", "http")).lower()
        connector: McpConnector
        if transport == "http":
            endpoint = config.get("endpoint")
            if not isinstance(endpoint, str):
                return ExecutionResult(status="FAILED", error_code="AI_MCP_CONFIG_INVALID", error_message="MCP endpoint is required")
            if self.http_connector_factory is None:
                return ExecutionResult(status="FAILED", error_code="AI_CONNECTOR_UNAVAILABLE", error_message="MCP HTTP connector is unavailable")
            settings = get_settings()
            auth_token = self._resolve_secret_token(secret_ref)
            if isinstance(auth_token, ExecutionResult):
                return auth_token
            # 主机白名单经 policy.source Port 提供（P2-14），缺失时退回配置；
            # 连接器 target_policy.allow_private_network 时放行白名单内目标的 http 明文形式
            policy = try_port("policy.source")
            allowed_hosts = policy.mcp_allowed_hosts() if policy is not None else settings.mcp_allowed_hosts
            target_policy = config.get("target_policy") if isinstance(config.get("target_policy"), dict) else {}
            allow_private = bool(config.get("allow_private_network") or target_policy.get("allow_private_network"))
            # 出站代理（连接器级三态）：缺省走全局，""=直连，URL=指定代理
            proxy = config.get("proxy") if "proxy" in config else None
            connector = self.http_connector_factory(endpoint, allowed_hosts=allowed_hosts, timeout_seconds=timeout_seconds, max_response_bytes=settings.mcp_max_response_bytes, auth_token=auth_token, extra_headers=self._identity_headers(identity), allow_private_network=allow_private, proxy=proxy if isinstance(proxy, str) else None)
        elif transport == "stdio":
            registry = self.stdio_registry
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
        except (McpTransportError, McpProtocolError) as exc:
            logger.warning("MCP tool execution failed: %s", exc)
            return ExecutionResult(status="FAILED", error_code="AI_MCP_TOOL_CALL_FAILED", error_message=str(exc) or "MCP tool execution failed")
        except (OSError, ValueError, RuntimeError):
            return ExecutionResult(status="FAILED", error_code="AI_SKILL_FAILED", error_message="MCP tool execution failed")
        finally:
            try:
                await connector.close()
            except Exception:  # noqa: BLE001 - 关闭失败不得覆盖执行结果
                logger.warning("failed to close MCP connector", exc_info=True)

    async def _execute_with_retry(self, executor: ToolExecutor, config: dict[str, Any], arguments: dict[str, Any], *, timeout_seconds: float, max_retries: int, backoff_seconds: float, guard_key: str | None = None) -> ExecutionResult:
        if guard_key is not None:
            gated = self._guard_gate(guard_key)
            if gated is not None:
                return gated
        result = ExecutionResult(status="FAILED", error_code="AI_SKILL_FAILED", error_message="tool execution failed")
        for attempt in range(max_retries + 1):
            try:
                if hasattr(executor, "execute_async"):
                    result = await asyncio.wait_for(executor.execute_async(config, arguments), timeout=timeout_seconds)
                else:
                    result = await asyncio.wait_for(asyncio.to_thread(executor.execute, config, arguments, timeout_seconds), timeout=timeout_seconds)
            except TimeoutError:
                result = ExecutionResult(status="FAILED", error_code="AI_SKILL_TIMEOUT", error_message=_TOOL_TIMEOUT_MESSAGE)
            except _EXECUTION_ERRORS:
                result = ExecutionResult(status="FAILED", error_code="AI_SKILL_FAILED", error_message=_TOOL_FAILURE_MESSAGE)
            except Exception:  # noqa: BLE001 - 执行器异常统一降级为失败结果并记账
                logger.exception("unexpected tool executor failure")
                result = ExecutionResult(status="FAILED", error_code="AI_SKILL_FAILED", error_message=_TOOL_FAILURE_MESSAGE)
            if result.status == "SUCCEEDED" or attempt == max_retries or not self._is_retryable(result):
                break
            await asyncio.sleep(min(backoff_seconds * (2 ** attempt), 30))
        if guard_key is not None:
            self._guard_record(guard_key, result)
        return result

    def _guard_gate(self, key: str) -> ExecutionResult | None:
        """熔断/配额闸门：通过返回 None（并计一次配额）；拒绝返回 FAILED 结果（不经执行器）。"""
        try:
            self._guard.check(f"executor.{key}")
        except ApiError as exc:
            return ExecutionResult(status="FAILED", error_code=exc.code, error_message=exc.message)
        return None

    def _guard_record(self, key: str, result: ExecutionResult) -> None:
        """熔断计数：SUCCEEDED 清零，FAILED 计失败；WAITING_HUMAN/CANCELLED 不计。

        闸门拒绝（熔断 OPEN / 配额超限）不是插件故障，不参与熔断计数——
        否则持续流量会在 OPEN 期间反复把拒绝记为失败、刷新冷却窗口，
        熔断器永远等不到恢复探测。
        """
        if result.error_code in (CIRCUIT_OPEN_CODE, QUOTA_EXCEEDED_CODE):
            return
        if result.status == "SUCCEEDED":
            self._guard.record(f"executor.{key}", succeeded=True)
        elif result.status == "FAILED":
            self._guard.record(f"executor.{key}", succeeded=False)

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
        ctx: SecurityContext,
        skill_version_id: UUID,
        arguments: dict[str, Any],
        task_id: UUID | None = None,
        timeout_seconds: float = 30,
    ) -> ExecutionResult:
        tenant_id = ctx.tenant_id
        user_id = ctx.user_id
        skill = db.get(SkillVersion, skill_version_id)
        if not skill or skill.tenant_id != tenant_id or skill.status != "PUBLISHED":
            raise ApiError("AI_SKILL_NOT_FOUND", "published skill version not found", 404)
        # 父级 Skill 已归档时版本不可再执行
        parent_skill = db.get(Skill, skill.skill_id)
        if parent_skill is None or parent_skill.status != "ACTIVE":
            raise ApiError("AI_SKILL_NOT_FOUND", "skill is unavailable", 404)
        if not self.permission_checker.has_all(db, user_id, skill.required_permissions):
            self._audit_permission_denied(ctx=ctx, resource_type="SKILL", resource_id=skill.id)
            raise ApiError("AI_PERMISSION_DENIED", _PERMISSION_MESSAGE, 403)
        links = db.query(SkillVersionTool).filter(SkillVersionTool.skill_version_id == skill.id).order_by(SkillVersionTool.sort_order, SkillVersionTool.created_at).all()
        if not links:
            result = ExecutionResult(status="SUCCEEDED", data={"instructions": skill.instructions})
            self._log(db, tenant_id, user_id, skill, None, task_id, result, trace_id=ctx.trace_id)
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
                    approval_provider=self._approvals,
                )
                result = ExecutionResult(status="WAITING_HUMAN", requires_confirmation=True, data={"confirmation_id": str(confirmation.id)})
                self._log(db, tenant_id, user_id, skill, tool, task_id, result, arguments=arguments, trace_id=ctx.trace_id)
                return result
            owner = db.get(Tool, tool.tool_id)
            # 父级 Tool 已归档时版本不可再执行
            if owner is None or owner.status != "ACTIVE":
                result = ExecutionResult(status="FAILED", error_code="AI_TOOL_NOT_FOUND", error_message="published tool version not found")
                results.append(result.data)
                self._log(db, tenant_id, user_id, skill, tool, task_id, result, arguments=arguments, trace_id=ctx.trace_id)
                return result
            executor = self._executors.get(owner.tool_type) if owner else None
            if owner and owner.tool_type == "COMPOSITE":
                result = ExecutionResult(status="FAILED", error_code="AI_COMPOSITE_ASYNC_REQUIRED", error_message="composite tool execution requires execute_async")
                self._log(db, tenant_id, user_id, skill, tool, task_id, result, arguments=arguments, trace_id=ctx.trace_id)
                results.append(result.data)
                return result
            if owner and owner.tool_type == "MCP":
                result = ExecutionResult(status="FAILED", error_code="AI_MCP_ASYNC_REQUIRED", error_message="MCP execution requires execute_async")
                results.append(result.data)
                self._log(db, tenant_id, user_id, skill, tool, task_id, result, arguments=arguments, trace_id=ctx.trace_id)
                if result.status != "SUCCEEDED":
                    return result
                continue
            if executor is None:
                result = ExecutionResult(status="FAILED", error_code="AI_EXECUTOR_UNAVAILABLE", error_message="executor is unavailable")
            else:
                gated = self._guard_gate(owner.tool_type)
                if gated is not None:
                    result = gated
                else:
                    try:
                        result = executor.execute(self._materialize_config(tool, connector, identity=self._identity_meta(ctx, task_id)), arguments, timeout_seconds)
                    except TimeoutError:
                        result = ExecutionResult(status="FAILED", error_code="AI_SKILL_TIMEOUT", error_message="tool execution timed out")
                    except _EXECUTION_ERRORS:
                        result = ExecutionResult(status="FAILED", error_code="AI_SKILL_FAILED", error_message="tool execution failed")
                    except Exception:  # noqa: BLE001
                        logger.exception("unexpected tool executor failure")
                        result = ExecutionResult(status="FAILED", error_code="AI_SKILL_FAILED", error_message="tool execution failed")
            result = self._validate_output(tool.output_schema, result)
            if executor is not None:
                self._guard_record(owner.tool_type, result)
            results.append(result.data)
            self._log(db, tenant_id, user_id, skill, tool, task_id, result, arguments=arguments, trace_id=ctx.trace_id)
            if result.status != "SUCCEEDED":
                return result
        return ExecutionResult(status="SUCCEEDED", data=results)

    async def execute_async(self, db: Session, ctx: SecurityContext, **kwargs: Any) -> ExecutionResult:
        """Execute sync and negotiated async tools through one audited gateway path."""
        skill_version_id = kwargs.pop("skill_version_id")
        tenant_id = ctx.tenant_id
        user_id = ctx.user_id
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
            self._audit_permission_denied(ctx=ctx, resource_type="SKILL", resource_id=skill.id)
            raise ApiError("AI_PERMISSION_DENIED", "required permissions are missing", 403)
        links = db.query(SkillVersionTool).filter(SkillVersionTool.skill_version_id == skill.id).order_by(SkillVersionTool.sort_order, SkillVersionTool.created_at).all()
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
                # 沙箱策略拒绝/资源超限/超时：网关侧独立事务留痕（v1.3 §4.2 决策 C）
                self._audit_sandbox_event(ctx=ctx, skill_version_id=skill.id, code=exc.code, message=exc.message)
            self._guard_record("MD_SCRIPT", result)
            self._log(db, tenant_id, user_id, skill, None, task_id, result, trace_id=ctx.trace_id)
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
                self._audit_permission_denied(ctx=ctx, resource_type="TOOL", resource_id=tool.id)
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
                    approval_provider=self._approvals,
                )
                result = ExecutionResult(status="WAITING_HUMAN", requires_confirmation=True, data={"confirmation_id": str(confirmation.id)})
                self._log(db, tenant_id, user_id, skill, tool, task_id, result, arguments=arguments, trace_id=ctx.trace_id)
                return result
            executor = self._executors.get(owner.tool_type)
            if owner.tool_type == "COMPOSITE":
                result = await self._execute_composite(
                    db, ctx, skill, tool, arguments, task_id, timeout_seconds, deadline_at,
                    cancellation_event, resume_checkpoint_id,
                )
                result = self._validate_output(tool.output_schema, result)
                self._log(db, tenant_id, user_id, skill, tool, task_id, result, arguments=arguments, trace_id=ctx.trace_id)
                if result.status != "SUCCEEDED":
                    return result
                results.append(result.data)
                continue
            if owner.tool_type == "MCP":
                gated = self._guard_gate(owner.tool_type)
                identity = self._identity_meta(ctx, task_id)
                result = gated if gated is not None else await self._execute_mcp(tool.config_snapshot, arguments, self._remaining_timeout(timeout_seconds, deadline_at), secret_ref=connector.secret_ref if connector else None, identity=identity)
                self._guard_record(owner.tool_type, result)
                self._log(db, tenant_id, user_id, skill, tool, task_id, result, arguments=arguments, trace_id=ctx.trace_id)
                if result.status != "SUCCEEDED":
                    return result
                results.append(result.data)
                continue
            if executor is None:
                result = ExecutionResult(status="FAILED", error_code="AI_EXECUTOR_UNAVAILABLE", error_message="executor is unavailable")
            else:
                result = await self._execute_with_retry(executor, self._materialize_config(tool, connector, identity=self._identity_meta(ctx, task_id)), arguments, timeout_seconds=min(self._remaining_timeout(timeout_seconds, deadline_at), tool.timeout_seconds), max_retries=tool.max_retries, backoff_seconds=tool.retry_backoff_seconds, guard_key=owner.tool_type)
            result = self._validate_output(tool.output_schema, result)
            self._log(db, tenant_id, user_id, skill, tool, task_id, result, arguments=arguments, trace_id=ctx.trace_id)
            if result.status != "SUCCEEDED":
                return result
            results.append(result.data)
        return ExecutionResult(status="SUCCEEDED", data=results or {"instructions": skill.instructions})

    async def _execute_composite(
        self,
        db: Session,
        ctx: SecurityContext,
        skill: SkillVersion,
        composite: ToolVersion,
        arguments: dict[str, Any],
        task_id: UUID | None,
        timeout_seconds: float,
        deadline_at: Any,
        cancellation_event: asyncio.Event | None,
        resume_checkpoint_id: UUID | None,
    ) -> ExecutionResult:
        """线性执行业务封装 Tool 的步骤（深度 1，无分支），每步复用网关安全链路。"""
        config = composite.config_snapshot or {}
        steps = config.get("steps")
        if not isinstance(steps, list) or not steps or len(steps) > 10:
            return ExecutionResult(status="FAILED", error_code="AI_COMPOSITE_INVALID", error_message="composite steps are invalid")
        inputs = arguments if isinstance(arguments, dict) else {}
        step_outputs: dict[str, Any] = {}
        for step in steps:
            if self._cancelled(cancellation_event):
                return ExecutionResult(status="CANCELLED", error_code="AI_TASK_NOT_CANCELABLE", error_message="task cancellation requested")
            if not isinstance(step, dict):
                return ExecutionResult(status="FAILED", error_code="AI_COMPOSITE_INVALID", error_message="composite step is invalid")
            key = str(step.get("key") or "").strip()
            if not key or key in step_outputs:
                return ExecutionResult(status="FAILED", error_code="AI_COMPOSITE_INVALID", error_message="composite step key is invalid")
            try:
                child = db.get(ToolVersion, UUID(str(step.get("tool_version_id"))))
            except (ValueError, TypeError):
                return ExecutionResult(status="FAILED", error_code="AI_COMPOSITE_INVALID", error_message="composite step references an invalid tool version")
            owner = db.get(Tool, child.tool_id) if child else None
            if not child or not owner or child.tenant_id != ctx.tenant_id or child.status != "PUBLISHED" or owner.status != "ACTIVE":
                return ExecutionResult(status="FAILED", error_code="AI_TOOL_NOT_FOUND", error_message=f"composite step tool is unavailable: {key}")
            # 注册期已禁止嵌套，这里再兜底一次，避免历史数据绕过
            if owner.tool_type == "COMPOSITE":
                return ExecutionResult(status="FAILED", error_code="AI_COMPOSITE_INVALID", error_message="nested composite tools are not allowed")
            # 子步骤需审批时，必须已有审批 checkpoint 才能继续（fail-closed）
            if child.requires_confirmation and task_id and not resume_checkpoint_id:
                return ExecutionResult(status="FAILED", error_code="AI_TOOL_CONFIRMATION_REQUIRED", error_message=f"composite step requires confirmation: {key}")
            connector = None
            connector_id = child.config_snapshot.get("connector_id")
            if connector_id:
                connector = db.get(Connector, connector_id)
                if not connector or connector.tenant_id != ctx.tenant_id or connector.status != "ENABLED":
                    return ExecutionResult(status="FAILED", error_code="AI_CONNECTOR_UNAVAILABLE", error_message=f"connector is unavailable: {key}")
            if not self.permission_checker.has_all(db, ctx.user_id, child.required_permissions):
                return ExecutionResult(status="FAILED", error_code="AI_PERMISSION_DENIED", error_message=_PERMISSION_MESSAGE)
            try:
                resolved = _resolve_step_arguments(step.get("arguments"), inputs, step_outputs, child.input_schema)
                self._validate_input(child.input_schema, resolved)
            except ApiError as exc:
                return ExecutionResult(status="FAILED", error_code=exc.code, error_message=exc.message)
            except (KeyError, IndexError, ValueError):
                return ExecutionResult(status="FAILED", error_code="AI_COMPOSITE_ARGUMENT_ERROR", error_message=f"composite step argument cannot be resolved: {key}")
            identity = self._identity_meta(ctx, task_id)
            if owner.tool_type == "MCP":
                gated = self._guard_gate(owner.tool_type)
                result = gated if gated is not None else await self._execute_mcp(
                    child.config_snapshot, resolved, self._remaining_timeout(timeout_seconds, deadline_at),
                    secret_ref=connector.secret_ref if connector else None, identity=identity,
                )
                self._guard_record(owner.tool_type, result)
            else:
                executor = self._executors.get(owner.tool_type)
                if executor is None:
                    result = ExecutionResult(status="FAILED", error_code="AI_EXECUTOR_UNAVAILABLE", error_message=f"executor is unavailable: {key}")
                else:
                    result = await self._execute_with_retry(
                        executor, self._materialize_config(child, connector, identity=identity), resolved,
                        timeout_seconds=min(self._remaining_timeout(timeout_seconds, deadline_at), child.timeout_seconds),
                        max_retries=child.max_retries, backoff_seconds=child.retry_backoff_seconds,
                        guard_key=owner.tool_type,
                    )
            result = self._validate_output(child.output_schema, result)
            self._log(db, ctx.tenant_id, ctx.user_id, skill, child, task_id, result, arguments=resolved, trace_id=ctx.trace_id)
            if result.status != "SUCCEEDED":
                return result
            step_outputs[key] = result.data
        output_path = config.get("output_path")
        if output_path:
            try:
                return ExecutionResult(status="SUCCEEDED", data=_select_composite_output(str(output_path), step_outputs))
            except (KeyError, IndexError, ValueError):
                return ExecutionResult(status="FAILED", error_code="AI_COMPOSITE_OUTPUT_ERROR", error_message="composite output path cannot be resolved")
        return ExecutionResult(status="SUCCEEDED", data={"steps": step_outputs})

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

    @staticmethod
    def _identity_meta(ctx: SecurityContext | None, task_id: UUID | None = None) -> dict[str, str] | None:
        """提取身份元数据（P2-5：SecurityContext 贯穿外部调用，委托链可审计）。"""
        if ctx is None:
            return None
        meta: dict[str, str] = {}
        if ctx.user_id:
            meta["user_id"] = str(ctx.user_id)
        if ctx.trace_id:
            meta["trace_id"] = ctx.trace_id
        if task_id is not None:
            meta["task_id"] = str(task_id)
        return meta or None

    @staticmethod
    def _identity_headers(identity: dict[str, str] | None) -> dict[str, str] | None:
        """身份元数据 → 出站头（HTTP / MCP 传输层统一 X-ZT-* 命名）。"""
        if not identity:
            return None
        headers: dict[str, str] = {}
        if identity.get("user_id"):
            headers["X-ZT-User-Id"] = str(identity["user_id"])
        if identity.get("trace_id"):
            headers["X-ZT-Trace-Id"] = str(identity["trace_id"])
        if identity.get("task_id"):
            headers["X-ZT-Task-Id"] = str(identity["task_id"])
        return headers or None

    def _materialize_config(self, tool: ToolVersion, connector: Connector | None, *, identity: dict[str, str] | None = None) -> dict[str, Any]:
        """构造运行时配置：解密连接器 Secret URI 供 {{secret}} 模板使用，并兜底 allowed_hosts。

        identity（P2-5）：执行期身份元数据，写入保留键 `_identity`（仅运行时存在，
        不落任何快照/持久化），供执行器（HTTP 出站头等）透传身份。
        """
        config = dict(tool.config_snapshot or {})
        if identity:
            config["_identity"] = identity
        if connector is not None and connector.connector_type == "DB":
            # DB 工具：连接信息以连接器当前 config 为准（运行时叠加，避免注册快照过期）；
            # allowed_tables 取当前连接器白名单，执行器会与注册快照求交集（fail-closed）。
            for key in ("driver", "host", "port", "database", "username"):
                if key in (connector.config or {}):
                    config[key] = connector.config[key]
            tables = (connector.config or {}).get("allowed_tables")
            if isinstance(tables, list):
                config["connector_allowed_tables"] = [str(table) for table in tables]
        if connector is not None and connector.connector_type in ("REST", "MCP"):
            # REST/MCP 工具：目标安全策略与出站代理以连接器实时配置为源头（保存即生效，
            # 无需重新登记工具）。proxy 三态：缺省=跟随全局；""=直连；URL=指定代理。
            if connector.target_policy:
                config["target_policy"] = {**(config.get("target_policy") or {}), **connector.target_policy}
            connector_config = connector.config or {}
            if "proxy" in connector_config:
                config["proxy"] = connector_config.get("proxy")
        if connector is not None and connector.connector_type == "REST":
            # 连接器级凭据头（前端连接器页配置，明文保存时已转 secret:// 引用）：
            # 覆盖导入工具 headers 里的同名 {{参数}} 模板项 → 一次配置对该连接器全部工具生效，
            # 无需重新发布工具版本；值在执行期由 HttpExecutor 经 SecretStore 解密
            auth_headers = (connector.config or {}).get("auth_headers")
            if isinstance(auth_headers, dict) and auth_headers:
                merged_headers = dict(config.get("headers") or {})
                merged_headers.update({str(k): str(v) for k, v in auth_headers.items()})
                config["headers"] = merged_headers
        if connector is not None and connector.secret_ref:
            try:
                config["secret_value"] = self._decrypt_secret(connector.secret_ref.removeprefix("secret://"))
            except Exception as exc:  # noqa: BLE001 - 解密实现可替换（KMS 等）
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
        # 执行侧与模型侧同源净化：悬空 $ref（OpenAPI 导入残留）会让 jsonschema 抛
        # RefResolutionError（非 ValidationError）导致任务 AI_TASK_FAILED
        clean = _sanitize_schema(schema)
        try:
            validate(arguments, clean)
        except ValidationError as exc:
            raise ApiError("AI_PARAM_INVALID", "tool input does not match its schema", 400, {"path": list(exc.path)}) from exc

    @staticmethod
    def _validate_output(schema: dict[str, Any] | None, result: ExecutionResult) -> ExecutionResult:
        if result.status != "SUCCEEDED" or not schema:
            return result
        clean = _sanitize_schema(schema)
        try:
            validate(result.data, clean)
        except ValidationError:
            return ExecutionResult(status="FAILED", error_code="AI_TASK_RESULT_INVALID", error_message="tool output does not match its schema")
        return result

    def _log(self, db: Session, tenant_id: UUID, user_id: UUID | None, skill: SkillVersion, tool: ToolVersion | None, task_id: UUID | None, result: ExecutionResult, arguments: dict | None = None, trace_id: str | None = None) -> None:
        # 入参可能含 DB 口令/HTTP 密钥：落库前按键脱敏，与字符串摘要策略保持一致
        safe_arguments = redact_mapping(arguments) if arguments else None
        db.add(SkillExecutionLog(tenant_id=tenant_id, actor_user_id=user_id, skill_version_id=skill.id, tool_version_id=tool.id if tool else None, task_id=task_id, status=result.status, result={**({"data": result.data} if result.data is not None else {}), **({"input": safe_arguments} if safe_arguments else {})}, error_code=result.error_code))
        # 审计事件经 interceptor Port 发射（P2 收口，契约红线1 审计不可关闭）；
        # 默认实现委托 record_audit，行为与直调完全一致；trace_id 必传，否则 /audit/by-trace 查不到技能执行
        self._interceptor.on_execution(db, tenant_id=tenant_id, actor_user_id=user_id, action="SKILL_EXECUTED", resource_type="SKILL_VERSION", resource_id=skill.id, summary="skill execution completed", detail={"status": result.status, "tool_version_id": str(tool.id) if tool else None}, trace_id=trace_id)
        db.flush()
