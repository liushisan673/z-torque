import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.errors import ApiError
from app.core.responses import error_response, success_response
from app.db.session import check_database
from app.api.router import router as api_router
from app.kernel.task.runtime import TaskRuntime
from app.kernel.builtin_executors import DbQueryExecutor, HttpExecutor
from app.kernel.db_executor import ConnectorDbExecutor
from app.kernel.skill_gateway import MdSkillScriptExecutor, McpExecutor, MockExecutor, SkillGateway
from app.kernel.mcp_connectors import StdioServerRegistry
from app.db.session import engine, SessionLocal
from app.config import get_settings
from app.kernel.model_gateway import MockModelGateway, OpenAICompatibleGateway
from app.kernel.md_skill_watcher import MdSkillWatcher
from app.models import Tenant

logger = logging.getLogger("z_torque")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO)
    gateway = SkillGateway()
    gateway.register(MockExecutor())
    gateway.register(HttpExecutor())
    gateway.register(DbQueryExecutor(engine))
    gateway.register(ConnectorDbExecutor())
    gateway.register(McpExecutor())
    gateway.register(MdSkillScriptExecutor())
    app.state.skill_gateway = gateway
    app.state.mcp_stdio_registry = StdioServerRegistry()
    gateway.stdio_registry = app.state.mcp_stdio_registry
    app.state.mcp_connectors = []
    settings = get_settings()
    app.state.md_skill_watchers = {}
    with SessionLocal() as bootstrap_db:
        tenants = bootstrap_db.query(Tenant).all()
        for tenant in tenants:
            watcher = MdSkillWatcher(settings.md_skills_dir, tenant.id, interval_seconds=settings.md_skill_scan_interval, enabled=settings.md_skill_watch_enabled)
            app.state.md_skill_watchers[str(tenant.id)] = watcher
            await watcher.start()
    model_gateway = MockModelGateway()
    if settings.model_base_url and settings.model_api_key:
        model_gateway = OpenAICompatibleGateway(settings.model_base_url, settings.model_api_key.get_secret_value(), default_model=settings.model_name)
    app.state.model_gateway = model_gateway
    runtime = TaskRuntime(gateway, model_gateway)
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


app = FastAPI(title="Z-Torque", version="0.1.0", lifespan=lifespan)

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
    return success_response(request, {"stage": "09", "features": ["health", "configuration", "chat", "model_gateway", "function_calling", "task_sse", "md_skill_discovery", "md_skill_versions", "connectors", "rest_connector", "readonly_db_connector", "mcp_connector", "working_memory", "short_term_memory", "file_upload", "file_parsing"], "md_skill": {"directory": str(settings.md_skills_dir), "script_enabled": settings.md_skill_script_enabled}, "model": {"configured": bool(settings.model_base_url and settings.model_api_key), "name": settings.model_name}, "memory": {"short_enabled": settings.memory_short_enabled, "long_enabled": settings.memory_long_enabled}, "files": {"storage_dir": str(settings.file_storage_dir), "max_upload_mb": settings.max_upload_size_mb}, "connectors": {"allowed_targets": settings.connector_allowed_targets, "max_response_bytes": settings.connector_max_response_bytes}})
