"""pytest 全局配置：将测试隔离到独立数据库。

根因：tests/integration/test_task_api.py 的 autouse fixture 会 delete(Tenant)，
直接清空真实 data/z_torque.db 中的 admin 用户与默认父 Agent，导致前端登录 401。

必须在任何 app.* 导入之前设置 DATABASE_URL，使 app.db.session 的 engine
绑定到独立的测试库，避免污染真实数据。
"""

import os

os.environ["DATABASE_URL"] = "sqlite:///./data/z_torque_test.db"

import pytest  # noqa: E402

from app.db.base import Base  # noqa: E402
from app.db.session import engine  # noqa: E402
import app.models  # noqa: E402, F401  # 注册所有模型到 Base.metadata


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """每个测试会话重建一次干净的测试库 schema。"""
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
    app.state.model_gateway = result.model_gateway
    app.state.mcp_stdio_registry = result.stdio_registry
    yield
