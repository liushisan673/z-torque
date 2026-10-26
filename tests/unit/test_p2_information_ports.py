"""P2 信息族/执行族 Port 收口测试：memory.working / long / short、object.store、sandbox.driver。"""

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.builtin_plugins.long_memory import InProcessKvLongMemory, LongMemoryPlugin
from app.builtin_plugins.manifest import PluginSdk
from app.builtin_plugins.object_store import LocalObjectStore, ObjectStorePlugin
from app.builtin_plugins.sandbox_driver import ProcessSandboxDriver, SandboxDriverPlugin
from app.builtin_plugins.short_memory import DbShortTermMemoryProvider, ShortMemoryPlugin
from app.builtin_plugins.working_memory import InProcessWorkingMemory, WorkingMemoryPlugin
from app.core.errors import ApiError
from app.db.session import SessionLocal
from app.kernel.ports.specs import PORT_SPECS
from app.kernel.registry.port_registry import PortRegistry, PortState
from app.models import Agent, Conversation, ShortTermMemory, Tenant, User
from app.security.passwords import hash_password
from tests.support.ddl import fk_off, fk_on

# ---- memory.working ----


def test_working_memory_lru_and_isolation():
    memory = InProcessWorkingMemory(max_entries=2)
    tenant, user, conv_a, conv_b = uuid4(), uuid4(), uuid4(), uuid4()
    memory.put(tenant, user, conv_a, {"v": 1})
    memory.put(tenant, user, conv_b, {"v": 2})
    memory.put(tenant, user, uuid4(), {"v": 3})  # 挤出最旧的 conv_a
    assert memory.get(tenant, user, conv_a) is None
    assert memory.get(tenant, user, conv_b) == {"v": 2}
    # 返回副本：调用方修改不影响缓存
    snapshot = memory.get(tenant, user, conv_b)
    snapshot["v"] = 999
    assert memory.get(tenant, user, conv_b) == {"v": 2}
    memory.delete(tenant, user, conv_b)
    assert memory.get(tenant, user, conv_b) is None


def test_working_memory_plugin_registers_port(tmp_path):
    spec = next(s for s in PORT_SPECS if s.name == "memory.working")
    registry = PortRegistry((spec,))
    WorkingMemoryPlugin().contribute(PluginSdk.of(WorkingMemoryPlugin(), registry=registry))
    registry.resolve()
    resolution = registry.resolution("memory.working")
    assert resolution.state == PortState.RESOLVED
    assert resolution.plugin == "working_memory"
    assert isinstance(resolution.instance, InProcessWorkingMemory)


# ---- memory.long ----


def test_long_memory_kv_semantics():
    memory = InProcessKvLongMemory()
    tenant, user, other = uuid4(), uuid4(), uuid4()
    memory.remember(tenant_id=tenant, user_id=user, key="preference", value=" concise")
    assert memory.recall(tenant_id=tenant, user_id=user, key="preference") == " concise"
    assert memory.recall(tenant_id=tenant, user_id=other, key="preference") is None
    memory.forget(tenant_id=tenant, user_id=user, key="preference")
    memory.forget(tenant_id=tenant, user_id=user, key="nonexistent")  # 幂等
    assert memory.recall(tenant_id=tenant, user_id=user, key="preference") is None


def test_long_memory_plugin_registers_port():
    spec = next(s for s in PORT_SPECS if s.name == "memory.long")
    registry = PortRegistry((spec,))
    LongMemoryPlugin().contribute(PluginSdk.of(LongMemoryPlugin(), registry=registry))
    registry.resolve()
    assert registry.resolution("memory.long").state == PortState.RESOLVED


# ---- memory.short ----


@pytest.fixture(autouse=True)
def cleanup_short_memory():
    yield
    with SessionLocal.begin() as db:
        fk_off(db)
        db.execute(delete(ShortTermMemory))
        db.execute(delete(User))
        db.execute(delete(Tenant))
        fk_on(db)


def test_short_memory_active_memory_readonly_query():
    with SessionLocal() as db:
        tenant = Tenant(name=f"p2s-{uuid4()}")
        db.add(tenant)
        db.flush()
        user = User(tenant_id=tenant.id, username=f"u-{uuid4()}", password_hash=hash_password("pass"))
        db.add(user)
        db.flush()
        # agent_id / conversation_id 都是真外键，必须建实体行：
        # 用 uuid4() 顶替只在"外键恰好没被强制"的方言上能过，PG 会直接 23503
        agent = Agent(tenant_id=tenant.id, owner_user_id=user.id, name=f"a-{uuid4()}", source_type="USER", status="ACTIVE")
        db.add(agent)
        db.flush()
        conversation = Conversation(tenant_id=tenant.id, owner_user_id=user.id, agent_id=agent.id)
        db.add(conversation)
        db.flush()

        item = ShortTermMemory(tenant_id=tenant.id, user_id=user.id, agent_id=agent.id, conversation_id=conversation.id, summary="s", source_start=0, source_end=1, source_message_ids=[], status="ACTIVE")
        db.add(item)
        db.flush()
        provider = DbShortTermMemoryProvider()
        assert provider.active_memory(db, tenant_id=tenant.id, user_id=user.id, agent_id=agent.id, conversation_id=conversation.id) is item
        assert provider.active_memory(db, tenant_id=tenant.id, user_id=user.id, agent_id=agent.id, conversation_id=uuid4()) is None
        db.rollback()


def test_short_memory_plugin_registers_port():
    spec = next(s for s in PORT_SPECS if s.name == "memory.short")
    registry = PortRegistry((spec,))
    ShortMemoryPlugin().contribute(PluginSdk.of(ShortMemoryPlugin(), registry=registry))
    registry.resolve()
    assert registry.resolution("memory.short").state == PortState.RESOLVED


# ---- object.store ----


def test_object_store_put_read_delete(tmp_path):
    store = LocalObjectStore(tmp_path / "files")
    result = store.put("a/b.txt", io.BytesIO(b"hello world"), max_bytes=1024)
    assert result.size_bytes == 11
    assert len(result.sha256) == 64
    assert store.local_path("a/b.txt").read_bytes() == b"hello world"
    store.delete("a/b.txt")
    store.delete("a/b.txt")  # 幂等
    assert not (tmp_path / "files" / "a" / "b.txt").exists()


def test_object_store_put_rejects_oversize_and_cleans_up(tmp_path):
    store = LocalObjectStore(tmp_path / "files")
    with pytest.raises(ApiError) as error:
        store.put("big.bin", io.BytesIO(b"x" * 100), max_bytes=10)
    assert error.value.code == "AI_FILE_TOO_LARGE"
    assert not (tmp_path / "files" / "big.bin").exists()


def test_object_store_rejects_path_escape(tmp_path):
    store = LocalObjectStore(tmp_path / "files")
    with pytest.raises(ApiError):
        store.local_path("../escape.txt")


def test_object_store_plugin_registers_port(tmp_path):
    spec = next(s for s in PORT_SPECS if s.name == "object.store")
    registry = PortRegistry((spec,))
    ObjectStorePlugin().contribute(PluginSdk.of(ObjectStorePlugin(), registry=registry, settings=SimpleNamespace(file_storage_dir=tmp_path / "files")))
    registry.resolve()
    resolution = registry.resolution("object.store")
    assert resolution.state == PortState.RESOLVED
    assert resolution.plugin == "object_store"
    assert isinstance(resolution.instance, LocalObjectStore)


# ---- sandbox.driver ----


def test_process_sandbox_driver_runs_script_and_returns_json(tmp_path):
    script = tmp_path / "main.py"
    script.write_text("import json, sys\nargs = json.load(sys.stdin)\nprint(json.dumps({'echo': args['x']}))\n", encoding="utf-8")
    result = asyncio.run(ProcessSandboxDriver().run(script_path=script, arguments={"x": 42}, cwd=tmp_path, timeout_seconds=30, max_output_bytes=65536))
    assert result == {"echo": 42}


def test_process_sandbox_driver_times_out(tmp_path):
    script = tmp_path / "loop.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    with pytest.raises(ApiError) as error:
        asyncio.run(ProcessSandboxDriver().run(script_path=script, arguments={}, cwd=tmp_path, timeout_seconds=1, max_output_bytes=65536))
    assert error.value.code == "AI_SKILL_TIMEOUT"


def test_sandbox_plugin_registers_port(tmp_path):
    spec = next(s for s in PORT_SPECS if s.name == "sandbox.driver")
    registry = PortRegistry((spec,))
    # v1.3：白名单在装配期强制校验，直构场景必须显式提供
    settings = SimpleNamespace(
        sandbox_allowed_roots=[str(tmp_path / "skills")],
        sandbox_rlimit_enabled=False,
        sandbox_max_processes=64,
        sandbox_max_file_bytes=64 * 1024 * 1024,
    )
    SandboxDriverPlugin().contribute(PluginSdk.of(SandboxDriverPlugin(), registry=registry, objects={}, settings=settings))
    registry.resolve()
    resolution = registry.resolution("sandbox.driver")
    assert resolution.state == PortState.RESOLVED
    assert resolution.plugin == "sandbox_driver"
    assert isinstance(resolution.instance, ProcessSandboxDriver)


def test_md_skill_runtime_has_no_builtin_sandbox_default(tmp_path):
    """实现归插件（标准插件可移植性）：内核不再自带默认执行后端。

    未注入 sandbox.driver Port 时必须显式拒绝执行，而不是在内核里偷偷跑一份默认实现——
    否则"插件是空壳、实现在内核"的问题会借着兜底路径复活。
    """
    from app.kernel.md_skill_runtime import MdSkillRuntime

    runtime = MdSkillRuntime(tmp_path, enabled=True)
    assert runtime._sandbox is None
    source = (Path(__file__).resolve().parents[2] / "app" / "kernel" / "md_skill_runtime.py").read_text(encoding="utf-8")
    # 内核文件不得再出现执行实现的特征符号（加固逻辑已整体下沉到插件）
    for marker in ("setrlimit", "killpg", "start_new_session", "create_subprocess_exec"):
        assert marker not in source
