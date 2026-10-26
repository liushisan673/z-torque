import logging
import re
import sys
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_optional_user
from app.api.router import router as api_router
from app.assembly import assemble, resolve_profile
from app.config import get_settings
from app.core.errors import ApiError
from app.core.responses import error_response, success_response
from app.db.session import SessionLocal, check_database, engine
from app.kernel.audit.service import set_request_trace_id
from app.kernel.capabilities import derive_stage
from app.kernel.md_skill_watcher import MdSkillWatcher
from app.kernel.registry.port_registry import PortState
from app.kernel.task.runtime import TaskRuntime
from app.models import Tenant, User
from app.security.permissions import is_admin_user

logger = logging.getLogger("z_torque")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    # 装配根统一装配：Profile → Plugin → Port 注册与解析校验（缺失必需 Port 时拒绝启动）。
    # Profile 由 settings.assembly_profile 显式派生（禁止在 assemble() 内隐式读环境变量）
    result = assemble(settings, engine, profile=resolve_profile(settings.assembly_profile))
    app.state.assembly_result = result
    # 能力面按"插件名"派生（v1.3 §7.3）：静态 features 清单不足以表达三个新能力
    app.state.assembly_plugins = tuple(manifest.name for manifest in result.plugins)
    # 插件经 sdk.publish 发布的领域服务（audit_export / a2a）转挂 app.state，
    # 受控 API 只读访问，不反向 import 插件包（阶段00 §3.8）
    for object_key, object_value in result.objects.items():
        setattr(app.state, object_key, object_value)
    app.state.port_registry = result.registry
    app.state.skill_gateway = result.skill_gateway
    app.state.model_gateway = result.model_gateway
    app.state.mcp_stdio_registry = result.stdio_registry
    app.state.mcp_connectors = []
    app.state.md_skill_watchers = {}
    with SessionLocal() as bootstrap_db:
        tenants = bootstrap_db.query(Tenant).all()
        for tenant in tenants:
            watcher = MdSkillWatcher(settings.md_skills_dir, tenant.id, interval_seconds=settings.md_skill_scan_interval, enabled=settings.md_skill_watch_enabled, session_factory=SessionLocal)
            app.state.md_skill_watchers[str(tenant.id)] = watcher
            await watcher.start()
    runtime = TaskRuntime(result.skill_gateway, result.model_gateway, queue=result.registry.get("queue.driver"), session_factory=SessionLocal, model_selector=result.registry.try_get("model.selector"), lock_provider=result.registry.try_get("lock.provider"), short_memory=result.registry.get("memory.short"))
    await runtime.start()
    app.state.task_runtime = runtime
    try:
        yield
    finally:
        for watcher in getattr(app.state, "md_skill_watchers", {}).values():
            await watcher.stop()
        for connector in getattr(app.state, "mcp_connectors", []):
            await connector.close()
        await runtime.stop()
        # 插件收尾（逆序）：释放导出线程池 / 归档定时器；插件不得自行注册 atexit
        result.shutdown()


# OpenAPI 文档中文分组说明：/docs 左侧 tag 树按此展示（name 必须与各路由文件的 tags=[...] 完全一致）
OPENAPI_TAG_DESCRIPTIONS = [
    {"name": "auth", "description": "认证与令牌：登录、刷新令牌、登出、当前用户信息"},
    {"name": "chat", "description": "对话发起：发送消息并触发智能体执行（含 SSE 流式接口）"},
    {"name": "messages", "description": "消息查询：会话内消息历史与消息详情"},
    {"name": "conversations", "description": "会话管理：创建、查询、重命名、归档与恢复、记忆摘要"},
    {"name": "agents", "description": "智能体管理：智能体与版本的创建、发布、归档、委派配置、技能绑定"},
    {"name": "tasks", "description": "任务与执行：任务状态、事件时间线、取消（含 SSE 事件订阅）"},
    {"name": "confirmations", "description": "人工确认：工具执行等敏感操作的批准与拒绝"},
    {"name": "skills-tools", "description": "技能与工具：技能/工具版本、指令内容、绑定关系与发布"},
    {"name": "md-skills", "description": "MD 技能包：SKILL.md / zip 上传解析与目录扫描导入"},
    {"name": "models", "description": "模型接入：供应商、模型与接入点三层管理及连通性测试"},
    {"name": "connectors", "description": "连接器：MCP / REST / 数据库连接管理、工具发现注册、连通性测试"},
    {"name": "files", "description": "文件管理：上传、状态查询、内容下载与删除"},
    {"name": "secrets", "description": "密钥存储：明文转 Secret 加密引用（明文不落库）"},
    {"name": "audit", "description": "审计日志：事件检索、详情与 Trace 链路查询（只读）"},
    {"name": "admin-iam", "description": "身份与权限管理：用户、角色与授权（仅管理员）"},
    {"name": "users", "description": "用户兼容接口：旧版用户查询与维护"},
    {"name": "roles", "description": "角色兼容接口：旧版角色与权限查询"},
]

app = FastAPI(
    title="Z-Torque 社区版 API",
    version="0.1.0",
    description=(
        "Z-Torque AI 能力开放平台。所有业务接口挂载在 `/api/z-torque` 前缀下，"
        "除登录/刷新外均需 `Authorization: Bearer <token>` 认证。"
        "响应统一为 `{ code, message, data }` 结构；"
        "SSE 流式接口（对话流、任务事件流）返回 `text/event-stream`。"
    ),
    openapi_tags=OPENAPI_TAG_DESCRIPTIONS,
    lifespan=lifespan,
)

# CORS：允许来源由 CORS_ALLOW_ORIGINS 配置（生产填控制台域名），默认仅本地 vite dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


# 外部 trace 只接受限长、受限字符集；非法值（换行/超长/任意文本）一律重新生成，
# 避免客户端伪造或写入超长值污染审计列（P2-17）
_TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    supplied = request.headers.get("X-Trace-ID", "")
    request.state.trace_id = supplied if _TRACE_ID_PATTERN.fullmatch(supplied) else str(uuid4())
    # 注入请求级 ContextVar：服务层内部审计点未显式传 trace 时由 record_audit 兜底回填
    set_request_trace_id(request.state.trace_id)
    response = await call_next(request)
    response.headers["X-Trace-ID"] = request.state.trace_id
    return response


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return error_response(request, exc)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, _: RequestValidationError):
    return error_response(request, ApiError("VALIDATION_ERROR", "request validation failed", 422))


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    logger.exception("unhandled request error", exc_info=exc)
    return error_response(request, ApiError("AI_INTERNAL_ERROR", "internal server error", 500))


@app.get("/health")
async def health(request: Request):
    """就绪探针：DB 不可达必须返回 503，否则 LB 无法摘除故障实例（P2-17）。"""
    try:
        database_ok = check_database()
    except Exception:
        logger.exception("health check failed")
        database_ok = False
    payload = success_response(request, {"status": "ok" if database_ok else "degraded", "database": database_ok})
    if not database_ok:
        return JSONResponse(status_code=503, content=payload)
    return payload


_FEATURE_PORT_DEPS = {"working_memory": "memory.working", "short_term_memory": "memory.short"}
# v1.3 三个新能力不新增 Port：必须按"插件是否已装配"派生（§7.3），
# 否则社区版也会宣称 a2a / audit_export / rlimit 沙箱可用
_FEATURE_PLUGIN_DEPS = {
    "sandbox": "sandbox_driver",
    "audit_export": "audit_export_archive",
    "a2a": "a2a_gateway",
}
_FEATURES = ["health", "configuration", "chat", "model_gateway", "function_calling", "model_selector", "task_sse", "task_recovery_and_cancel", "delegation", "md_skill_discovery", "md_skill_versions", "md_skill_watch", "connectors", "rest_connector", "readonly_db_connector", "mcp_connector", "secret_provider", "working_memory", "short_term_memory", "file_upload", "file_parsing", "audit_query", "admin_iam", "plugin_circuit_breaker", "plugin_sdk", "module_assembly", "sandbox", "audit_export", "a2a"]


def _available_features(registry, plugins: tuple[str, ...] = ()) -> list[str]:
    """features 必须反映 Port 真实解析状态：依赖的 Port 不可用时不能继续宣称可用（P2-14）。"""
    if registry is None:
        return list(_FEATURES)
    available: list[str] = []
    for feature in _FEATURES:
        port_name = _FEATURE_PORT_DEPS.get(feature)
        if port_name is None:
            available.append(feature)
            continue
        resolution = registry.resolution(port_name)
        if resolution is not None and resolution.state == PortState.RESOLVED:
            available.append(feature)
    for feature, plugin_name in _FEATURE_PLUGIN_DEPS.items():
        if feature in available and plugin_name not in plugins:
            available.remove(feature)
    return available


def _sandbox_capability(registry, plugins: tuple[str, ...], settings) -> dict:
    """沙箱能力如实上报：未实现级别禁止上报（§7.3）。"""
    resolution = registry.resolution("sandbox.driver") if registry is not None else None
    assembled = "sandbox_driver" in plugins and resolution is not None and resolution.state == PortState.RESOLVED
    if not assembled:
        return {"available": False, "impl": None, "isolation_level": "none"}
    label = str(getattr(resolution, "label", "") or "")
    if "container" in label:
        level = "container"
    elif settings.sandbox_rlimit_enabled and sys.platform != "win32":
        level = "rlimit"
    else:
        level = "none"
    return {"available": True, "impl": label or "builtin_process", "isolation_level": level}


def _audit_export_capability(plugins: tuple[str, ...], settings) -> dict:
    available = "audit_export_archive" in plugins
    if not available:
        return {"available": False, "formats": [], "archive": False}
    return {
        "available": True,
        "formats": list(settings.audit_export_formats),
        "archive": settings.audit_archive_schedule_hours > 0,
    }


def _a2a_capability(plugins: tuple[str, ...], settings) -> dict:
    available = "a2a_gateway" in plugins
    return {"available": available, "enabled": bool(available and settings.a2a_enabled), "max_depth": settings.a2a_max_depth}


@app.get("/api/z-torque/capabilities")
async def capabilities(request: Request, user: User | None = Depends(get_optional_user), db: Session = Depends(get_db)):
    settings = get_settings()
    # Port 能力来自注册表真实解析状态（阶段00 §3.5），不再是静态清单；
    # stage 由装配状态推导（P2-5），不再硬编码
    registry = getattr(request.app.state, "port_registry", None)
    assembled_plugins = getattr(request.app.state, "assembly_plugins", ())
    ports = registry.status() if registry is not None else []
    model_resolution = registry.resolution("model.provider") if registry is not None else None
    model_implementation = model_resolution.label if model_resolution is not None else None
    # 敏感细节（绝对路径、出站白名单）仅管理员可见（P2-15）：
    # 该接口此前无需认证即可返回部署路径与放行目标，等于给探测者送地图。
    # 匿名访问时 user 为 None（该端点保留公开可读，供前端探测能力），必须显式判定，
    # 否则会因访问 user.tenant_id 抛 AttributeError 变成 500。
    is_admin = user is not None and is_admin_user(db, tenant_id=user.tenant_id, user_id=user.id)
    md_skill = {"script_enabled": settings.md_skill_script_enabled}
    files = {"max_upload_mb": settings.max_upload_size_mb}
    connectors = {"max_response_bytes": settings.connector_max_response_bytes}
    if is_admin:
        md_skill["directory"] = str(settings.md_skills_dir)
        files["storage_dir"] = str(settings.file_storage_dir)
        connectors["allowed_targets"] = settings.connector_allowed_targets
    return success_response(request, {"stage": derive_stage(registry), "features": _available_features(registry, assembled_plugins), "ports": ports, "sandbox": _sandbox_capability(registry, assembled_plugins, settings), "audit_export": _audit_export_capability(assembled_plugins, settings), "a2a": _a2a_capability(assembled_plugins, settings), "md_skill": md_skill, "model": {"configured": bool(settings.model_base_url and settings.model_api_key), "name": settings.model_name, "implementation": model_implementation}, "memory": {"short_enabled": settings.memory_short_enabled, "long_enabled": settings.memory_long_enabled}, "files": files, "connectors": connectors})
