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


@router.get("")
async def list_connectors(request: Request, user: ReadUser, db: DbSession):
    items = db.scalars(select(Connector).where(Connector.tenant_id == user.tenant_id).order_by(Connector.created_at)).all()
    return success_response(request, [serialize(x) for x in items])


@router.post("")
async def create_connector(payload: ConnectorCreateRequest, request: Request, user: ManageUser, db: DbSession):
    if payload.secret_ref and not payload.secret_ref.startswith("secret://"):
        raise ApiError("AI_PARAM_INVALID", "secret_ref must use Secret URI", 400)
    item = Connector(tenant_id=user.tenant_id, created_by=user.id, **payload.model_dump(), status="ENABLED")
    db.add(item); db.flush()
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_CREATED", resource_type="CONNECTOR", resource_id=item.id, summary="connector created")
    db.commit(); db.refresh(item)
    return success_response(request, serialize(item))


@router.get("/{connector_id}")
async def get_connector(connector_id: UUID, request: Request, user: ReadUser, db: DbSession):
    return success_response(request, serialize(get_owned(db, user, connector_id)))


@router.patch("/{connector_id}")
async def patch_connector(connector_id: UUID, payload: ConnectorPatchRequest, request: Request, user: ManageUser, db: DbSession):
    item = get_owned(db, user, connector_id)
    values = payload.model_dump(exclude_unset=True)
    if "secret_ref" in values and values["secret_ref"] and not values["secret_ref"].startswith("secret://"):
        raise ApiError("AI_PARAM_INVALID", "secret_ref must use Secret URI", 400)
    for key, value in values.items(): setattr(item, key, value)
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_UPDATED", resource_type="CONNECTOR", resource_id=item.id, summary="connector updated", detail={"fields": list(values)})
    db.commit(); db.refresh(item)
    return success_response(request, serialize(item))


@router.post("/{connector_id}/disable")
async def disable_connector(connector_id: UUID, request: Request, user: ManageUser, db: DbSession):
    item = get_owned(db, user, connector_id); item.status = "DISABLED"
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_DISABLED", resource_type="CONNECTOR", resource_id=item.id, summary="connector disabled")
    db.commit()
    return success_response(request, serialize(item))


@router.post("/{connector_id}/enable")
async def enable_connector(connector_id: UUID, request: Request, user: ManageUser, db: DbSession):
    item = get_owned(db, user, connector_id)
    if item.status == "ARCHIVED":
        raise ApiError("AI_REQUEST_INVALID", "archived connector cannot be enabled", 409)
    item.status = "ENABLED"
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_ENABLED", resource_type="CONNECTOR", resource_id=item.id, summary="connector enabled")
    db.commit()
    return success_response(request, serialize(item))


@router.post("/{connector_id}/archive")
async def archive_connector(connector_id: UUID, request: Request, user: ManageUser, db: DbSession):
    item = get_owned(db, user, connector_id)
    item.status = "ARCHIVED"
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_ARCHIVED", resource_type="CONNECTOR", resource_id=item.id, summary="connector archived")
    db.commit()
    return success_response(request, serialize(item))


@router.delete("/{connector_id}")
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


@router.post("/{connector_id}/test")
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
    finished = datetime.now(timezone.utc)
    result["latency_ms"] = int((finished - started).total_seconds() * 1000)
    result["checked_at"] = iso_utc(finished)
    result["trace_id"] = trace_id_from(request)
    test_detail = {"status": result["status"], "latency_ms": result["latency_ms"]}
    if "tables" in result:
        test_detail["table_count"] = len(result["tables"])
    if "tools" in result:
        test_detail["tool_count"] = len(result["tools"])
    record_audit(db, tenant_id=user.tenant_id, actor_user_id=user.id, action="CONNECTOR_TESTED", resource_type="CONNECTOR", resource_id=item.id, summary="connector test completed", detail=test_detail)
    db.commit()
    return success_response(request, result)


@router.post("/{connector_id}/discover")
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


@router.post("/{connector_id}/tools/register")
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


@router.get("/{connector_id}/tools")
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
