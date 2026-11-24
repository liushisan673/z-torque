"""pytest 全局配置：将测试隔离到独立数据库。

根因：tests/integration/test_task_api.py 的 autouse fixture 会 delete(Tenant)，
直接清空真实 data/z_torque.db 中的 admin 用户与默认父 Agent，导致前端登录 401。

必须在任何 app.* 导入之前设置 DATABASE_URL，使 app.db.session 的 engine
绑定到独立的测试库，避免污染真实数据。
"""

import os

# 方言验证（如 PostgreSQL）必须**显式**通过 ZTORQUE_TEST_DATABASE_URL 指定，
# 绝不读取普通 DATABASE_URL：避免有人带着生产连接串跑测试，被 drop_all 清库。
os.environ["DATABASE_URL"] = os.environ.get("ZTORQUE_TEST_DATABASE_URL", "sqlite:///./data/z_torque_test.db")

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

# 插件私有表（方案 b）：模型随插件目录，需显式 import 才能进 create_all 的 metadata
import app.builtin_plugins.audit_export_archive.models  # noqa: E402, F401
import app.models  # noqa: E402, F401  # 注册所有模型到 Base.metadata
from app.db.base import Base  # noqa: E402
from app.db.session import engine  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """每个测试会话重建一次干净的测试库 schema。"""
    # FTS 虚表不在 ORM metadata 中，需显式删除，避免上一次运行的旧结构残留
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS zr_knowledge_fts"))
        try:
            connection.execute(text("CREATE VIRTUAL TABLE zr_knowledge_fts USING fts5(chunk_id, text, tokenize='unicode61')"))
        except Exception:  # noqa: BLE001 - 无 FTS5 的 sqlite3 走同构普通表（LIKE 降级）
            connection.execute(text("CREATE TABLE zr_knowledge_fts (chunk_id VARCHAR(36), text TEXT)"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(scope="session", autouse=True)
def _assembled_app_state():
    """ASGITransport 不触发 lifespan：为经 API 层的测试装配 Port 注册表。

    仅做装配（deps 依赖 identity.provider 等 Port）；不启动 TaskRuntime /
    MdSkillWatcher 等长生命周期组件，由各测试按需构造。
    """
    from app.assembly import assemble  # noqa: E402
    from app.config import get_settings  # noqa: E402
    from app.main import app  # noqa: E402

    result = assemble(get_settings(), engine)
    app.state.port_registry = result.registry
    app.state.skill_gateway = result.skill_gateway
    # capabilities 依赖装配插件名派生能力面（与 main.py lifespan 一致）
    app.state.assembly_plugins = tuple(manifest.name for manifest in result.plugins)
    # 决策（阶段13）：生产装配不再在“未配置模型”时降级为 MockModelGateway。
    # 测试需要可预测、不触网的模型行为，故在此显式注入 —— 这是 Mock 的唯一注入点，
    # 绝不出现在生产装配路径（app/builtin_plugins/plugins.py）。
    # MockExecutor / MockModelGateway 均定义在 tests/support/fakes.py（TEST-ONLY），
    # 只允许测试 DI 注入；禁止回挂生产装配或生产插件模块。
    from tests.support.fakes import MockModelGateway  # noqa: E402

    app.state.model_gateway = MockModelGateway()
    app.state.mcp_stdio_registry = result.stdio_registry
    yield
