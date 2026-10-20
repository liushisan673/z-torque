import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.assembly import assemble
from app.core.errors import ApiError
from app.core.responses import error_response, success_response
from app.db.session import check_database
from app.api.router import router as api_router
from app.kernel.capabilities import derive_stage
from app.kernel.task.runtime import TaskRuntime
from app.kernel.md_skill_watcher import MdSkillWatcher
from app.db.session import SessionLocal, engine
from app.config import get_settings
from app.models import Tenant

logger = logging.getLogger("z_torque")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    # 装配根统一装配：Profile → Plugin → Port 注册与解析校验（缺失必需 Port 时拒绝启动）
    result = assemble(settings, engine)
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

# Dev CORS: allow console(vite) to call API with cookies/Authorization headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    request.state.trace_id = request.headers.get("X-Trace-ID", str(uuid4()))
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
    database_ok = check_database()
    return success_response(request, {"status": "ok" if database_ok else "degraded", "database": database_ok})


@app.get("/api/z-torque/capabilities")
async def capabilities(request: Request):
    settings = get_settings()
    # Port 能力来自注册表真实解析状态（阶段00 §3.5），不再是静态清单；
    # stage 由装配状态推导（P2-5），不再硬编码
    registry = getattr(request.app.state, "port_registry", None)
    ports = registry.status() if registry is not None else []
    model_resolution = registry.resolution("model.provider") if registry is not None else None
    model_implementation = model_resolution.label if model_resolution is not None else None
    return success_response(request, {"stage": derive_stage(registry), "features": ["health", "configuration", "chat", "model_gateway", "function_calling", "model_selector", "task_sse", "task_recovery_and_cancel", "delegation", "md_skill_discovery", "md_skill_versions", "md_skill_watch", "connectors", "rest_connector", "readonly_db_connector", "mcp_connector", "secret_provider", "working_memory", "short_term_memory", "file_upload", "file_parsing", "audit_query", "admin_iam", "plugin_circuit_breaker", "plugin_sdk", "module_assembly"], "ports": ports, "md_skill": {"directory": str(settings.md_skills_dir), "script_enabled": settings.md_skill_script_enabled}, "model": {"configured": bool(settings.model_base_url and settings.model_api_key), "name": settings.model_name, "implementation": model_implementation}, "memory": {"short_enabled": settings.memory_short_enabled, "long_enabled": settings.memory_long_enabled}, "files": {"storage_dir": str(settings.file_storage_dir), "max_upload_mb": settings.max_upload_size_mb}, "connectors": {"allowed_targets": settings.connector_allowed_targets, "max_response_bytes": settings.connector_max_response_bytes}})
