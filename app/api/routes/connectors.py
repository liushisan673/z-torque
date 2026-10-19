from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import ApiError
from app.core.responses import success_response, trace_id_from
from app.core.servertime import iso_utc
from app.db.session import get_db
from app.kernel.audit.service import record_audit
from app.kernel.skill_service import _snapshot_hash
from app.models import Connector, ConnectorTool, Tool, ToolVersion, User
from app.schemas.connector import (
    ConnectorCreateRequest,
    ConnectorOpenapiImportRequest,
    ConnectorPatchRequest,
    ConnectorToolRegisterRequest,
)
from app.security.permissions import require_permissions

router = APIRouter(prefix="/connectors", tags=["connectors"])

CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[Session, Depends(get_db)]
# 读操作（列表/详情/工具/探测）只需 connector.read；变更操作需 connector.manage
ReadUser = Annotated[User, Depends(require_permissions(["connector.read"]))]
ManageUser = Annotated[User, Depends(require_permissions(["connector.manage"]))]

# MCP 探测（initialize 握手 + tools/list）单次超时上限，避免 hung 服务拖死前端
MCP_PROBE_TIMEOUT_SECONDS = 10.0
# REST 测试连接：单次超时上限（与 MCP 对齐，避免 hung 服务拖死前端）
REST_PROBE_TIMEOUT_SECONDS = 8.0


def _mcp_error_reason(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:200] or "unknown error"


async def _probe_mcp(config: dict, secret_ref: str | None = None) -> dict:
    """对 MCP 连接做真实探测：initialize 握手 + tools/list，返回 tools/list 原始结果。

    仅支持 HTTP 传输（Streamable HTTP）；失败抛 ApiError 或底层 Mcp*Error。
    """
    from app.config import get_settings
    from app.builtin_plugins.mcp import StreamableHttpMcpConnector
    from app.security.secret_store import SecretStore, SecretStoreError

    settings = get_settings()
    config = config or {}
    endpoint = str(config.get("endpoint") or "").strip()
    if not endpoint:
        raise ApiError("AI_PARAM_INVALID", "MCP 连接缺少 endpoint 配置", 400)
    raw_timeout = config.get("timeout_seconds")
    try:
        timeout = float(raw_timeout) if raw_timeout else settings.mcp_timeout_seconds
    except (TypeError, ValueError):
        timeout = settings.mcp_timeout_seconds
    auth_token = None
    if secret_ref:
        try:
            auth_token = SecretStore().decrypt(secret_ref.removeprefix("secret://"))
        except SecretStoreError as exc:
            raise ApiError("AI_CONNECTOR_SECRET_INVALID", "连接器凭证无法解密", 503) from exc
    connector = StreamableHttpMcpConnector(
        endpoint,
        allowed_hosts=settings.mcp_allowed_hosts,
        timeout_seconds=max(min(timeout, MCP_PROBE_TIMEOUT_SECONDS), 1.0),
        max_response_bytes=settings.mcp_max_response_bytes,
        auth_token=auth_token,
    )
    try:
        await connector.negotiate()
        return await connector.list_tools()
    finally:
        await connector.close()


def _rest_auth_headers(config: dict, secret_ref: str | None) -> tuple[dict[str, str], bool]:
    """根据 config.auth_mode 与 secret_ref 构造 REST 探测请求头。

    约定 auth_mode：
      - "Bearer Token"：Authorization: Bearer <token>
      - "API Key Header"：<auth_header>: <token>（默认 X-API-Key）
      - "Basic Auth"：Authorization: Basic <token>（token 已含 user:pass，平台不做解析）
      - "匿名（需管理员允许）"：不附加鉴权头

    返回 (headers, auth_used)。auth_used=True 表示确实附加了凭证，便于审计/前端诚实提示。
    """
    mode = str(config.get("auth_mode") or "Bearer Token")
    headers: dict[str, str] = {}
    if not secret_ref:
        return headers, False
    from app.security.secret_store import SecretStore, SecretStoreError
    try:
        token = SecretStore().decrypt(secret_ref.removeprefix("secret://"))
    except SecretStoreError as exc:
        raise ApiError("AI_CONNECTOR_SECRET_INVALID", "连接器凭证无法解密", 503) from exc
    if mode == "Bearer Token":
        headers["Authorization"] = f"Bearer {token}"
    elif mode == "API Key Header":
        header = str(config.get("auth_header") or "Authorization").strip() or "Authorization"
        headers[header] = token
    elif mode == "Basic Auth":
        headers["Authorization"] = f"Basic {token}"
    # 匿名模式：故意不发任何凭证
    return headers, bool(headers)


async def _probe_rest(config: dict, secret_ref: str | None) -> dict:
    """对 REST 连接做真实探测：按 auth_mode 附加凭证 → HEAD/GET 一次 → 回报 status/header/latency。

    返回字典至少含 http_status、method_used、auth_used；失败抛 ApiError。
    401/403 仍视为「远端可达」，单独以 status=auth_required 表达，便于前端诚实提示。
    """
    import asyncio
    import httpx

    config = config or {}
    base_url = str(config.get("base_url") or "").strip()
    if not base_url:
        # 没有 base_url：诚实返回，让前端明确「未发起实际网络请求」
        return {
            "probed": False,
            "reason": "missing base_url",
            "auth_used": False,
        }
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    # 内网地址绕过环境代理：否则会拿到代理返回的 502，被误判成"对方服务挂了"
    bypass_proxy = _is_private_address(parsed.hostname)
    if parsed.scheme not in ("http", "https"):
        raise ApiError("AI_PARAM_INVALID", "REST 连接 base_url 必须以 http:// 或 https:// 开头", 400)
    if not parsed.netloc:
        raise ApiError("AI_PARAM_INVALID", "REST 连接 base_url 缺少主机", 400)
    # 默认禁止探测本机地址：防止 SSRF 打到平台自身或云元数据端点（169.254.169.254 等私有段同理）。
    # 本地联调需显式放行：app_debug=True，或把主机写进 settings.connector_allowed_targets。
    from urllib.parse import urlparse as _urlparse
    _p = _urlparse(base_url)
    if _p.hostname in {"127.0.0.1", "localhost", "::1"} or (
        _p.hostname and _p.hostname.startswith("169.254.")
    ):
        from app.config import get_settings
        settings = get_settings()
        allowed = {str(t).strip().lower() for t in (settings.connector_allowed_targets or [])}
        if not settings.app_debug and str(_p.hostname).lower() not in allowed:
            raise ApiError(
                "AI_CONNECTOR_FORBIDDEN_HOST",
                "REST 探测不允许指向本机/链路本地地址；本地联调请在 connector_allowed_targets 中显式放行",
                400,
            )

    raw_timeout = config.get("timeout_seconds")
    try:
        timeout = float(raw_timeout) if raw_timeout else REST_PROBE_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        timeout = REST_PROBE_TIMEOUT_SECONDS
    timeout = max(min(timeout, REST_PROBE_TIMEOUT_SECONDS), 1.0)
    verify_tls = config.get("verify_tls", True)
    if verify_tls in (False, "false", "False"):
        verify_tls = False
    else:
        verify_tls = True

    headers, auth_used = _rest_auth_headers(config, secret_ref)
    headers.setdefault("User-Agent", "z-torque-connector-probe/1.0")
    headers.setdefault("Accept", "application/json, text/plain;q=0.9, */*;q=0.5")

    # 内网地址强制绕过环境代理（trust_env=False + proxy=None 双保险）：
    # 否则 httpx 会把 192.168.x / 10.x 的请求交给 127.0.0.1:12000 这类本机代理，
    # 代理连不上目标就回 502，被误读成"对方服务 502"，实际是本机网络不通。
    client_kwargs: dict = {"follow_redirects": False, "timeout": timeout, "verify": verify_tls}
    if bypass_proxy:
        client_kwargs["trust_env"] = False
        client_kwargs["proxy"] = None

    started = datetime.now(timezone.utc)
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            # 优先 HEAD：部分网关不允许，则回退 GET
            try:
                resp = await client.request("HEAD", base_url, headers=headers)
                method_used = "HEAD"
            except httpx.HTTPError:
                resp = await client.request("GET", base_url, headers=headers)
                method_used = "GET"
            # 部分服务 HEAD 返回 405/501，回退 GET
            if resp.status_code in (405, 501) and method_used == "HEAD":
                resp = await client.request("GET", base_url, headers=headers)
                method_used = "GET"
            status = resp.status_code
            server = resp.headers.get("server") or resp.headers.get("Server") or ""
            content_type = resp.headers.get("content-type") or ""
            # 代理特征头：用于把"代理返回的 502"和"目标服务返回的 502"区分开
            proxy_hint = bool(resp.headers.get("proxy-connection") or resp.headers.get("via"))
            await resp.aclose()
    except httpx.TimeoutException as exc:
        # 直连超时：目标不可达（内网场景多为网段不通/服务未监听），给出可操作的排查提示
        raise ApiError(
            "AI_CONNECTOR_UNAVAILABLE",
            f"连接 {parsed.netloc} 超时（{int(timeout)}s，已{'绕过' if bypass_proxy else '经由'}代理）："
            f"探测由后端主机发起，请确认目标已启动并监听，且后端到该网段路由可达",
            504,
        ) from exc
    except httpx.ConnectError as exc:
        raise ApiError(
            "AI_CONNECTOR_UNAVAILABLE",
            f"无法连接 {parsed.netloc}（已{'绕过' if bypass_proxy else '经由'}代理）：{_rest_error_reason(exc)}",
            502,
        ) from exc
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise ApiError("AI_CONNECTOR_UNAVAILABLE", f"REST 探测失败：{_rest_error_reason(exc)}", 502) from exc

    finished = datetime.now(timezone.utc)
    elapsed_ms = int((finished - started).total_seconds() * 1000)
    if 200 <= status < 400:
        verdict = "ready"
    elif status in (401, 403):
        verdict = "auth_required"  # 鉴权被拒：远端可达，凭证待修
    else:
        # 4xx/5xx：区分"目标服务返回"与"中间代理返回"，后者必须点名代理，避免冤枉对方服务
        origin = "中间代理" if proxy_hint else "远端服务"
        raise ApiError(
            "AI_CONNECTOR_UNAVAILABLE",
            f"{origin}返回 {status}（{method_used} {parsed.netloc}"
            f"；server={server[:60] or 'unknown'}"
            f"；{'已绕过代理' if bypass_proxy else '经环境代理'}）"
            + ("：该状态码来自代理而非目标服务，请检查本机 http_proxy 设置" if proxy_hint else ""),
            502,
        )
    return {
        "probed": True,
        "verdict": verdict,
        "http_status": status,
        "method_used": method_used,
        "auth_used": auth_used,
        "server": server[:120],
        "content_type": content_type[:120],
        "elapsed_ms": elapsed_ms,
        "proxy_bypassed": bypass_proxy,
    }


def _rest_error_reason(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:200] or "unknown error"


def _is_private_address(host: str | None) -> bool:
    """判断主机是否内网/本机地址（RFC1918 10/8、172.16/12、192.168/16，含 loopback 与 link-local）。

    用途：内网探测必须绕过 http_proxy。本机常驻的 http_proxy 多为公网出口或 VPN 客户端
    （实测 iKuuu/Clash 等监听 127.0.0.1:12000），拿 192.168.x、10.x 去问代理必然失败，
    且代理会返回一个 502 —— 那不是目标服务挂了，是代理自己连不上，极具误导性。
    """
    import ipaddress

    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host.strip().strip("[]"))
    except ValueError:
        return False  # 域名按公网处理，照常走代理
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def _mask_secret_ref(value: str | None) -> str | None:
    """脱敏 Secret URI：仅保留引用形态与末 4 位，任何场景不返回完整引用/明文。"""
    if not value:
        return None
    tail = value[-4:] if len(value) > 8 else "****"
    return f"secret://••••{tail}"


def serialize(item: Connector) -> dict:
    return {
        "id": str(item.id),
        "name": item.name,
        "connector_type": item.connector_type,
        "status": item.status,
        "config": item.config,
        "secret_configured": bool(item.secret_ref),
        "secret_ref_masked": _mask_secret_ref(item.secret_ref),
        "target_policy": item.target_policy,
        "description": item.description,
        "created_at": iso_utc(item.created_at),
    }


def get_owned(db: Session, user: User, connector_id: UUID) -> Connector:
    item = db.scalar(select(Connector).where(Connector.id == connector_id, Connector.tenant_id == user.tenant_id))
    if not item:
        raise ApiError("AI_RESOURCE_NOT_FOUND", "connector not found", 404)
    return item


@router.get("", summary="获取连接器列表")
async def list_connectors(request: Request, user: ReadUser, db: DbSession):
    items = db.scalars(select(Connector).where(Connector.tenant_id == user.tenant_id).order_by(Connector.created_at)).all()
    return success_response(request, [serialize(x) for x in items])


@router.post("", summary="创建连接器")
async def create_connector(payload: ConnectorCreateRequest, request: Request, user: ManageUser, db: DbSession):
    if payload.secret_ref and not payload.secret_ref.startswith("secret://"):
        raise ApiError("AI_PARAM_INVALID", "secret_ref must use Secret URI", 400)
    item = Connector(tenant_id=user.tenant_id, created_by=user.id, **payload.model_dump(), status="ENABLED")
    db.add(item); db.flush()
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_CREATED", resource_type="CONNECTOR", resource_id=item.id, summary="connector created")
    db.commit(); db.refresh(item)
    return success_response(request, serialize(item))


@router.get("/{connector_id}", summary="获取连接器详情")
async def get_connector(connector_id: UUID, request: Request, user: ReadUser, db: DbSession):
    return success_response(request, serialize(get_owned(db, user, connector_id)))


@router.patch("/{connector_id}", summary="更新连接器")
async def patch_connector(connector_id: UUID, payload: ConnectorPatchRequest, request: Request, user: ManageUser, db: DbSession):
    item = get_owned(db, user, connector_id)
    values = payload.model_dump(exclude_unset=True)
    if "secret_ref" in values and values["secret_ref"] and not values["secret_ref"].startswith("secret://"):
        raise ApiError("AI_PARAM_INVALID", "secret_ref must use Secret URI", 400)
    for key, value in values.items(): setattr(item, key, value)
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_UPDATED", resource_type="CONNECTOR", resource_id=item.id, summary="connector updated", detail={"fields": list(values)})
    db.commit(); db.refresh(item)
    return success_response(request, serialize(item))


@router.post("/{connector_id}/disable", summary="停用连接器")
async def disable_connector(connector_id: UUID, request: Request, user: ManageUser, db: DbSession):
    item = get_owned(db, user, connector_id); item.status = "DISABLED"
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_DISABLED", resource_type="CONNECTOR", resource_id=item.id, summary="connector disabled")
    db.commit()
    return success_response(request, serialize(item))


@router.post("/{connector_id}/enable", summary="启用连接器")
async def enable_connector(connector_id: UUID, request: Request, user: ManageUser, db: DbSession):
    item = get_owned(db, user, connector_id)
    if item.status == "ARCHIVED":
        raise ApiError("AI_REQUEST_INVALID", "archived connector cannot be enabled", 409)
    item.status = "ENABLED"
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_ENABLED", resource_type="CONNECTOR", resource_id=item.id, summary="connector enabled")
    db.commit()
    return success_response(request, serialize(item))


@router.post("/{connector_id}/archive", summary="归档连接器")
async def archive_connector(connector_id: UUID, request: Request, user: ManageUser, db: DbSession):
    item = get_owned(db, user, connector_id)
    item.status = "ARCHIVED"
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_ARCHIVED", resource_type="CONNECTOR", resource_id=item.id, summary="connector archived")
    db.commit()
    return success_response(request, serialize(item))


@router.delete("/{connector_id}", summary="删除连接器")
async def delete_connector(connector_id: UUID, request: Request, user: ManageUser, db: DbSession):
    item = get_owned(db, user, connector_id)
    if item.status != "ARCHIVED":
        raise ApiError("AI_REQUEST_INVALID", "connector must be archived before deletion", 409)
    # 绑定检查：仍注册有工具的连接器不允许删除，需先删除对应工具
    linked = db.scalar(select(func.count(ConnectorTool.id)).where(ConnectorTool.connector_id == item.id)) or 0
    if linked:
        raise ApiError("AI_RESOURCE_IN_USE", "connector still has registered tools; delete them first", 409)
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_DELETED", resource_type="CONNECTOR", resource_id=item.id, summary="connector deleted")
    db.delete(item)
    db.commit()
    return success_response(request, {"id": str(connector_id), "deleted": True})


@router.post("/{connector_id}/test", summary="测试连接器连接")
async def test_connector(connector_id: UUID, request: Request, user: ReadUser, db: DbSession):
    item = get_owned(db, user, connector_id)
    if item.status != "ENABLED":
        raise ApiError("AI_CONNECTOR_UNAVAILABLE", "connector is disabled", 503)
    started = datetime.now(timezone.utc)
    result = {
        "connector_id": str(item.id),
        "status": "ready",
        "connector_type": item.connector_type,
    }
    # DB 连接：真实连库（SELECT 1 + 表清单）；MCP：真实握手（initialize + tools/list）；其余类型为就绪性检查
    if item.connector_type == "DB":
        from app.kernel.db_executor import probe_database
        from app.security.secret_store import SecretStore, SecretStoreError

        password = None
        if item.secret_ref:
            try:
                password = SecretStore().decrypt(item.secret_ref.removeprefix("secret://"))
            except SecretStoreError as exc:
                raise ApiError("AI_CONNECTOR_SECRET_INVALID", "连接器凭证无法解密", 503) from exc
        try:
            tables = probe_database(dict(item.config or {}), password)
        except Exception as exc:
            reason = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else "unknown error"
            record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_TESTED", resource_type="CONNECTOR", resource_id=item.id, summary="connector test failed", detail={"error": reason})
            db.commit()
            raise ApiError("AI_CONNECTOR_UNAVAILABLE", f"数据库连接失败：{reason}", 503)
        result["database"] = str((item.config or {}).get("database") or "")
        result["tables"] = tables
    elif item.connector_type == "MCP":
        from app.kernel.ports.connector import McpProtocolError, McpTransportError

        transport = str((item.config or {}).get("transport") or "http").lower()
        if transport not in ("http", ""):
            raise ApiError("AI_REQUEST_INVALID", "当前仅支持 HTTP 传输的 MCP 连接探测", 400)
        try:
            listing = await _probe_mcp(dict(item.config or {}), item.secret_ref)
        except ApiError:
            raise
        except (McpProtocolError, McpTransportError, OSError, ValueError, RuntimeError) as exc:
            reason = _mcp_error_reason(exc)
            record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_TESTED", resource_type="CONNECTOR", resource_id=item.id, summary="connector test failed", detail={"error": reason})
            db.commit()
            raise ApiError("AI_CONNECTOR_UNAVAILABLE", f"MCP 连接失败：{reason}", 503) from exc
        discovered = [t for t in (listing.get("tools") or []) if isinstance(t, dict) and t.get("name")]
        result["endpoint"] = str((item.config or {}).get("endpoint") or "")
        result["tools"] = [{"name": t.get("name"), "description": str(t.get("description") or "")} for t in discovered]
    elif item.connector_type == "REST":
        # REST：真实发起一次网络请求（按 auth_mode 附加凭证），不再"假成功"
        try:
            probe = await _probe_rest(dict(item.config or {}), item.secret_ref)
        except ApiError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            reason = _rest_error_reason(exc)
            record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_TESTED", resource_type="CONNECTOR", resource_id=item.id, summary="connector test failed", detail={"error": reason})
            db.commit()
            raise ApiError("AI_CONNECTOR_UNAVAILABLE", f"REST 探测失败：{reason}", 502) from exc
        if not probe.get("probed"):
            # 缺 base_url：诚实告知"未发起实际网络请求"，status 仍 ready，但前端会标 partial
            result["status"] = "ready"
            result["partial"] = True
            result["warning"] = "未配置 base_url，已跳过实际网络探测"
            result["auth_used"] = False
        else:
            result["status"] = probe["verdict"]  # ready | auth_required
            result["endpoint"] = str((item.config or {}).get("base_url") or "")
            result["http_status"] = probe["http_status"]
            result["response_method"] = probe["method_used"]
            result["auth_used"] = probe["auth_used"]
            result["server"] = probe.get("server") or ""
            result["content_type"] = probe.get("content_type") or ""
            result["proxy_bypassed"] = bool(probe.get("proxy_bypassed"))
            result["partial"] = probe["verdict"] == "auth_required"
            if probe["verdict"] == "auth_required":
                result["warning"] = "远端可达，但鉴权被拒（401/403）"
    finished = datetime.now(timezone.utc)
    result["latency_ms"] = int((finished - started).total_seconds() * 1000)
    result["checked_at"] = iso_utc(finished)
    result["trace_id"] = trace_id_from(request)
    test_detail = {"status": result["status"], "latency_ms": result["latency_ms"]}
    if "tables" in result:
        test_detail["table_count"] = len(result["tables"])
    if "tools" in result:
        test_detail["tool_count"] = len(result["tools"])
    if result.get("http_status"):
        test_detail["http_status"] = result["http_status"]
        test_detail["auth_used"] = result.get("auth_used", False)
    if result.get("partial"):
        test_detail["partial"] = True
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_TESTED", resource_type="CONNECTOR", resource_id=item.id, summary="connector test completed", detail=test_detail)
    db.commit()
    return success_response(request, result)


@router.post("/{connector_id}/discover", summary="发现连接器工具")
async def discover_connector(connector_id: UUID, request: Request, user: ReadUser, db: DbSession):
    item = get_owned(db, user, connector_id)
    if item.status != "ENABLED":
        raise ApiError("AI_CONNECTOR_UNAVAILABLE", "connector is disabled", 503)
    if item.connector_type != "MCP":
        return success_response(request, {"connector_id": str(item.id), "tools": []})
    from app.kernel.ports.connector import McpProtocolError, McpTransportError

    try:
        listing = await _probe_mcp(dict(item.config or {}), item.secret_ref)
    except ApiError:
        raise
    except (McpProtocolError, McpTransportError, OSError, ValueError, RuntimeError) as exc:
        raise ApiError("AI_CONNECTOR_UNAVAILABLE", f"MCP 工具发现失败：{_mcp_error_reason(exc)}", 503) from exc
    # MCP 规范为 camelCase（inputSchema），统一转本项目 snake_case 返回
    tools = [
        {
            "name": t.get("name"),
            "description": str(t.get("description") or ""),
            "input_schema": t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {},
        }
        for t in (listing.get("tools") or [])
        if isinstance(t, dict) and t.get("name")
    ]
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_DISCOVERED", resource_type="CONNECTOR", resource_id=item.id, summary="connector tools discovered", detail={"tool_count": len(tools)})
    db.commit()
    return success_response(request, {"connector_id": str(item.id), "tools": tools, "requires_approval": True})


@router.post("/{connector_id}/tools/register", summary="注册连接器工具")
async def register_connector_tool(connector_id: UUID, payload: ConnectorToolRegisterRequest, request: Request, user: ManageUser, db: DbSession):
    from app.kernel.db_executor import validate_sql_statement

    connector = get_owned(db, user, connector_id)
    if connector.status != "ENABLED":
        raise ApiError("AI_CONNECTOR_UNAVAILABLE", "connector is disabled", 503)
    if payload.tool_type == "DB_SQL":
        # DB 工具：必须注册在 DB 连接上；SQL 模板注册期即校验（单条语句 + 表白名单 + 参数声明）
        if connector.connector_type != "DB":
            raise ApiError("AI_PARAM_INVALID", "DB_SQL 工具只能注册在数据库连接上", 400)
        allowed = sorted({str(t).strip() for t in (connector.config or {}).get("allowed_tables") or [] if str(t).strip()})
        if not allowed:
            raise ApiError("AI_REQUEST_INVALID", "请先完成数据库连接探测并保存允许的表（allowed_tables）", 400)
        sql_template = (payload.sql_template or "").strip()
        if not sql_template:
            raise ApiError("AI_PARAM_INVALID", "SQL 模板不能为空", 400)
        driver = str((connector.config or {}).get("driver") or "postgresql").lower()
        try:
            _sql, params, is_write = validate_sql_statement(sql_template, set(allowed), driver=driver, allow_write=True)
        except ValueError as exc:
            raise ApiError("AI_PARAM_INVALID", str(exc), 400)
        declared = set((payload.input_schema or {}).get("properties") or {})
        missing = [name for name in params if name not in declared]
        if missing:
            raise ApiError("AI_PARAM_INVALID", f"SQL 模板参数未在输入 Schema 中声明：{', '.join(missing)}", 400)
        raw_max_rows = payload.config.get("max_rows")
        max_rows = raw_max_rows if isinstance(raw_max_rows, int) and not isinstance(raw_max_rows, bool) else 100
        config_snapshot = {"kind": "sql", "sql_template": sql_template, "allowed_tables": allowed, "max_rows": min(max(max_rows, 1), 1000), "write": is_write, "connector_id": str(connector.id)}
        tool_type = "DB_SQL"
    else:
        config = {**connector.config, **payload.config, "connector_id": str(connector.id), "connector_type": connector.connector_type}
        config["target_policy"] = connector.target_policy
        if payload.external_name:
            config["external_name"] = payload.external_name
        # MCP 工具执行期按 config.tool_name 发起 tools/call；缺省取外部名，再退回注册名
        if payload.tool_type == "MCP":
            config["tool_name"] = payload.external_name or payload.name
        config_snapshot = config
        tool_type = payload.tool_type
    tool = Tool(tenant_id=user.tenant_id, owner_user_id=user.id, name=payload.name, description=payload.description, tool_type=tool_type, source_type="CONNECTOR", status="ACTIVE", official=False)
    db.add(tool); db.flush()
    # 写入类 SQL 语句（INSERT/UPDATE/DELETE）强制人工审批：执行前必须经确认暂停/重放流程
    version = ToolVersion(tenant_id=user.tenant_id, tool_id=tool.id, version_number=1, status="PUBLISHED", config_snapshot=config_snapshot, input_schema=payload.input_schema, output_schema=payload.output_schema, required_permissions=payload.required_permissions, requires_confirmation=is_write if payload.tool_type == "DB_SQL" else False, snapshot_hash=_snapshot_hash(config_snapshot))
    db.add(version); db.flush(); tool.current_published_version_id = version.id
    link = ConnectorTool(tenant_id=user.tenant_id, connector_id=connector.id, tool_id=tool.id, external_name=payload.external_name, approved=True)
    db.add(link)
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_TOOL_REGISTERED", resource_type="CONNECTOR", resource_id=connector.id, summary="connector tool registered", detail={"tool_id": str(tool.id), "tool_version_id": str(version.id)})
    db.commit()
    return success_response(request, {"tool_id": str(tool.id), "tool_version_id": str(version.id), "connector_id": str(connector.id)})


@router.get("/{connector_id}/tools", summary="获取连接器工具列表")
async def connector_tools(connector_id: UUID, request: Request, user: ReadUser, db: DbSession):
    get_owned(db, user, connector_id)
    links = db.scalars(select(ConnectorTool).where(ConnectorTool.connector_id == connector_id, ConnectorTool.tenant_id == user.tenant_id)).all()
    tool_ids = [x.tool_id for x in links]
    tool_map: dict[UUID, Tool] = {}
    if tool_ids:
        rows = db.scalars(select(Tool).where(Tool.id.in_(tool_ids))).all()
        tool_map = {t.id: t for t in rows}
    return success_response(request, [
        {
            "id": str(x.tool_id),
            "external_name": x.external_name,
            "approved": x.approved,
            "name": tool_map[x.tool_id].name if x.tool_id in tool_map else None,
            "tool_type": tool_map[x.tool_id].tool_type if x.tool_id in tool_map else None,
            "description": tool_map[x.tool_id].description if x.tool_id in tool_map else None,
            "registered_at": iso_utc(x.created_at),
        }
        for x in links
    ])


# ---- OpenAPI 批量导入（仅 REST） ----
# 常见调用方：
# 1. URL 模式：客户端传 https://...spec.json，服务端拉取解析
# 2. JSON 模式：客户端直接把 OpenAPI 3.x 文档 POST 进来
# 每条 path+method → 1 个 HTTP_API 工具，复用 register_connector_tool 的内部逻辑
_DEFAULT_OPENAPI_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_MAX_OPENAPI_TOOLS_PER_IMPORT = 200


def _build_input_schema_from_params(params: list[dict]) -> dict:
    """把 OpenAPI parameters[] 简化为本项目 input_schema（含 path/query/header 的扁平 properties）。

    真实 OpenAPI parameters 还会包含 schema.in / schema.type 等；这里只取最常见形态。
    """
    properties: dict[str, dict] = {}
    required: list[str] = []
    for p in params or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        schema = p.get("schema") if isinstance(p.get("schema"), dict) else {}
        prop: dict = {"type": str(schema.get("type") or "string")}
        if "description" in p:
            prop["description"] = str(p["description"])[:200]
        if "enum" in schema and isinstance(schema["enum"], list):
            prop["enum"] = schema["enum"]
        properties[name] = prop
        if p.get("required"):
            required.append(name)
    out: dict = {"type": "object", "properties": properties}
    if required:
        out["required"] = required
    return out


def _safe_tool_name(raw: str, prefix: str, seen: set[str]) -> str:
    """生成合法 tool name：以字母开头，仅含字母数字下划线，连字符转下划线，最长 64 字符。

    同一连接器下唯一：已占用名加 _2/_3... 后缀。
    """
    import re
    s = re.sub(r"[^A-Za-z0-9_]+", "_", f"{prefix}{raw}").strip("_")
    if not s:
        s = "tool"
    if not re.match(r"^[A-Za-z_]", s):
        s = f"t_{s}"
    s = s[:64].rstrip("_") or "tool"
    base, n = s, 1
    while s.lower() in seen:
        n += 1
        suffix = f"_{n}"
        s = (base[: 64 - len(suffix)] + suffix)
    seen.add(s.lower())
    return s


@router.post("/{connector_id}/tools/import-openapi", summary="导入OpenAPI工具")
async def import_openapi_tools(connector_id: UUID, payload: ConnectorOpenapiImportRequest, request: Request, user: ManageUser, db: DbSession):
    """把外部 OpenAPI 3.x 文档一次性转成 N 个 HTTP_API 工具。

    仅适用 REST 连接器（其它类型直接 400）；重复的 operationId/名称会被自动去重。
    """
    connector = get_owned(db, user, connector_id)
    if connector.connector_type != "REST":
        raise ApiError("AI_PARAM_INVALID", "OpenAPI 批量导入仅适用于 REST 连接器", 400)
    if connector.status != "ENABLED":
        raise ApiError("AI_CONNECTOR_UNAVAILABLE", "connector is disabled", 503)

    # 1) 拿 spec
    spec: dict
    if payload.source == "url":
        if not payload.url:
            raise ApiError("AI_PARAM_INVALID", "url 模式必须传 url", 400)
        import httpx
        from urllib.parse import urlparse
        parsed = urlparse(payload.url.strip())
        if parsed.scheme != "https" or not parsed.netloc:
            raise ApiError("AI_PARAM_INVALID", "url 必须为 https", 400)
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                resp = await client.get(payload.url.strip(), headers={"Accept": "application/json, application/yaml;q=0.9"})
                resp.raise_for_status()
                raw = resp.text
        except httpx.HTTPError as exc:
            raise ApiError("AI_OPENAPI_FETCH_FAILED", f"下载 spec 失败：{_rest_error_reason(exc)}", 502) from exc
        try:
            import json as _json
            try:
                spec = _json.loads(raw)
            except _json.JSONDecodeError:
                # 极简 YAML 兜底（仅支持 mappings+sequences+strings，复杂 spec 仍请传 JSON）
                import yaml  # type: ignore
                spec = yaml.safe_load(raw)
        except Exception as exc:
            raise ApiError("AI_OPENAPI_PARSE_FAILED", f"解析 spec 失败：{_rest_error_reason(exc)}", 400) from exc
    else:
        if not isinstance(payload.spec, dict):
            raise ApiError("AI_PARAM_INVALID", "json 模式必须传 spec 对象", 400)
        spec = payload.spec

    # 2) 校验 OpenAPI 形态
    if not isinstance(spec.get("paths"), dict) or not spec["paths"]:
        raise ApiError("AI_PARAM_INVALID", "OpenAPI 文档缺少 paths 字段", 400)
    openapi = str(spec.get("openapi") or spec.get("swagger") or "")
    if openapi.startswith("3."):
        spec_version = "openapi3"
    elif openapi.startswith("2."):
        spec_version = "swagger2"
    else:
        # 宽松：无法识别时按 openapi3 形态走（多数现代服务都是 3.x）
        spec_version = "openapi3"

    # 3) 已经在这个连接器下登记过的 external_name，避免重复
    existing = db.scalars(select(ConnectorTool).where(ConnectorTool.connector_id == connector.id)).all()
    existing_external = {(x.external_name or "").lower() for x in existing}
    seen: set[str] = set(existing_external)

    only_tags = {t.strip() for t in (payload.only_tags or []) if t and t.strip()}
    allowed_methods = {m.upper() for m in (payload.only_methods or [])} or set(_DEFAULT_OPENAPI_METHODS)

    base_url = str((connector.config or {}).get("base_url") or "").rstrip("/")
    if not base_url:
        # 用 spec 内的 servers[0].url 作 fallback
        servers = spec.get("servers")
        if isinstance(servers, list) and servers and isinstance(servers[0], dict):
            base_url = str(servers[0].get("url") or "").rstrip("/")

    registered: list[dict] = []
    skipped: list[dict] = []

    for path, methods in spec["paths"].items():
        if not isinstance(methods, dict):
            continue
        # 处理 OpenAPI 3.x 的 parameters（path 级）
        path_level_params = methods.get("parameters") if isinstance(methods.get("parameters"), list) else []
        for method, op in methods.items():
            method_upper = method.upper()
            if method_upper not in allowed_methods:
                continue
            if not isinstance(op, dict):
                continue
            tags = op.get("tags") or []
            if only_tags and not (set(map(str, tags)) & only_tags):
                continue
            op_params = list(path_level_params) + list(op.get("parameters") or [])
            op_id = str(op.get("operationId") or f"{method_upper}_{path}").strip()
            tool_name = _safe_tool_name(op_id, payload.name_prefix, seen)
            full_url = (base_url + path) if base_url else path
            description = str(op.get("summary") or op.get("description") or f"{method_upper} {path}")[:200]
            headers = {"Accept": "application/json"}
            query_params: dict[str, str] = {}
            body_schema: dict = {}
            for p in op_params:
                if not isinstance(p, dict):
                    continue
                pin = str(p.get("in") or "query")
                pname = str(p.get("name") or "").strip()
                if not pname:
                    continue
                if pin == "header":
                    headers[pname] = f"{{{{{pname}}}}}"
                elif pin == "query":
                    query_params[pname] = f"{{{{{pname}}}}}"
                # path 参数：写入 URL（用变量占位，但 HTTP API 当前执行期不解析 path 参数 → 列入 query 兜底）
                elif pin == "path":
                    query_params[pname] = f"{{{{{pname}}}}}"
            # requestBody：取 application/json 的 schema 描述（执行期按原始 body 透传）
            rb = op.get("requestBody")
            if isinstance(rb, dict):
                content = rb.get("content") if isinstance(rb.get("content"), dict) else {}
                json_content = content.get("application/json") if isinstance(content.get("application/json"), dict) else {}
                js = json_content.get("schema") if isinstance(json_content.get("schema"), dict) else None
                if js:
                    body_schema = js
            input_schema = _build_input_schema_from_params(op_params)
            if body_schema:
                # 把 body 视作整体参数，便于上层 LLM 显式传入
                input_schema.setdefault("properties", {})["__body__"] = {**body_schema, "description": "request body (JSON object)"}
            if body_schema and isinstance(body_schema.get("required"), list):
                input_schema.setdefault("required", []).append("__body__")

            if len(registered) >= _MAX_OPENAPI_TOOLS_PER_IMPORT:
                skipped.append({"name": op_id, "reason": f"单次导入上限 {_MAX_OPENAPI_TOOLS_PER_IMPORT}"}); continue
            config_snapshot = {
                "method": method_upper,
                "url": full_url,
                "headers": headers,
                "queryParams": query_params,
                "connector_id": str(connector.id),
                "connector_type": "REST",
                "openapi": {"path": path, "method": method_upper, "version": spec_version, "operation_id": op_id},
            }
            config_snapshot.update({k: v for k, v in (connector.config or {}).items() if k in ("target_policy",)})
            if "target_policy" in config_snapshot:
                pass  # 已用 dict.update 合并，避免覆盖

            tool = Tool(
                tenant_id=user.tenant_id, owner_user_id=user.id,
                name=tool_name, description=description,
                tool_type="HTTP", source_type="CONNECTOR", status="ACTIVE", official=False,
            )
            db.add(tool); db.flush()
            version = ToolVersion(
                tenant_id=user.tenant_id, tool_id=tool.id, version_number=1, status="PUBLISHED",
                config_snapshot=config_snapshot, input_schema=input_schema, output_schema={}, required_permissions=[],
                requires_confirmation=False, snapshot_hash=_snapshot_hash(config_snapshot),
            )
            db.add(version); db.flush(); tool.current_published_version_id = version.id
            link = ConnectorTool(tenant_id=user.tenant_id, connector_id=connector.id, tool_id=tool.id, external_name=op_id, approved=True)
            db.add(link)
            registered.append({
                "tool_id": str(tool.id), "tool_version_id": str(version.id),
                "operation_id": op_id, "method": method_upper, "path": path,
                "name": tool_name,
            })

    record_audit(
        db, tenant_id=user.tenant_id, actor_user_id=user.id,
        action="CONNECTOR_OPENAPI_IMPORTED", resource_type="CONNECTOR", resource_id=connector.id,
        summary="openapi tools imported", detail={"registered": len(registered), "skipped": len(skipped), "source": payload.source},
    )
    db.commit()
    return success_response(request, {
        "connector_id": str(connector.id),
        "registered": registered,
        "skipped": skipped,
        "total": len(registered) + len(skipped),
    })
