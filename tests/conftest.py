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
