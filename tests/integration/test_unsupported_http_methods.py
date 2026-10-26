"""存量「方法不可执行」HTTP 工具的归档口径。

入口闸门只能拦住新导入/新注册；历史库里已存在的 PUT/PATCH/DELETE 工具需要一次性归档。
这里锁定脚本的行为：只归档 HTTP + 非 GET/POST + ACTIVE，保留版本与连接器登记，且幂等。
"""

from __future__ import annotations

from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models import (
    AuditLog,
    Connector,
    ConnectorTool,
    SkillVersionTool,
    Tenant,
    Tool,
    ToolVersion,
    User,
)
from scripts.archive_unsupported_http_tools import (
    archive,
    collect_unsupported,
    composite_dependents,
)
from tests.support.ddl import fk_off, fk_on


def _reset(db) -> None:
    fk_off(db)
    for model in (SkillVersionTool, ConnectorTool, ToolVersion, Tool, Connector):
        db.execute(delete(model))
    fk_on(db)


def _seed(db, method: str, name: str) -> tuple[Tool, ToolVersion]:
    tenant = db.scalars(select(Tenant).order_by(Tenant.name)).first()
    if tenant is None:
        tenant = Tenant(name="mig-tenant")
        db.add(tenant)
        db.flush()
    user = db.scalars(select(User).where(User.tenant_id == tenant.id)).first()
    if user is None:
        user = User(tenant_id=tenant.id, username="mig-user", password_hash="x")
        db.add(user)
        db.flush()
    connector = Connector(
        tenant_id=tenant.id,
        created_by=user.id,
        name="CRM-TEST",
        connector_type="REST",
        status="ENABLED",
        config={"base_url": "https://crm.example.com"},
        target_policy={},
    )
    db.add(connector)
    db.flush()

    tool = Tool(
        tenant_id=tenant.id,
        owner_user_id=user.id,
        name=name,
        tool_type="HTTP",
        source_type="CONNECTOR",
        status="ACTIVE",
        tags=[],
        is_business_tool=False,
    )
    db.add(tool)
    db.flush()
    version = ToolVersion(
        tenant_id=tenant.id,
        tool_id=tool.id,
        version_number=1,
        status="PUBLISHED",
        config_snapshot={"method": method, "url": f"https://crm.example.com/{name}"},
        input_schema={},
        required_permissions=[],
        requires_confirmation=False,
        snapshot_hash="h",
    )
    db.add(version)
    db.flush()
    tool.current_published_version_id = version.id
    db.add(ConnectorTool(tenant_id=tenant.id, connector_id=connector.id, tool_id=tool.id, external_name=name, approved=True))
    return tool, version


def test_archive_only_touches_unsupported_active_http_tools():
    with SessionLocal.begin() as db:
        _reset(db)
        put_tool, put_version = _seed(db, "PUT", "updateUser")
        post_tool, _post_version = _seed(db, "POST", "batchDeleteUsers")

    with SessionLocal() as db:
        targets = collect_unsupported(db)
        assert [tool.id for tool, _v, _m in targets] == [put_tool.id]
        assert archive(db, targets) == 1

    with SessionLocal() as db:
        assert db.get(Tool, put_tool.id).status == "ARCHIVED"
        # 合法方法不受影响
        assert db.get(Tool, post_tool.id).status == "ACTIVE"
        # 不删除：版本与连接器登记仍在，审计链完整
        assert db.get(ToolVersion, put_version.id) is not None
        assert db.scalar(select(ConnectorTool).where(ConnectorTool.tool_id == put_tool.id)) is not None
        actions = set(db.scalars(select(AuditLog.action).where(AuditLog.resource_id == put_tool.id)).all())
        assert "TOOL_ARCHIVED" in actions
        # 幂等：第二次执行没有可归档对象
        assert collect_unsupported(db) == []


def test_active_composite_referencing_archived_child_is_reported():
    with SessionLocal.begin() as db:
        _reset(db)
        put_tool, put_version = _seed(db, "PUT", "updateUser")
        tenant_id = put_tool.tenant_id
        owner_id = put_tool.owner_user_id
        combo = Tool(
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            name="combo_update",
            tool_type="COMPOSITE",
            source_type="CONNECTOR",
            status="ACTIVE",
            tags=[],
            is_business_tool=True,
        )
        db.add(combo)
        db.flush()
        combo_version = ToolVersion(
            tenant_id=tenant_id,
            tool_id=combo.id,
            version_number=1,
            status="PUBLISHED",
            config_snapshot={"steps": [{"key": "s1", "tool_version_id": str(put_version.id), "arguments": {}}]},
            input_schema={},
            required_permissions=[],
            requires_confirmation=False,
            snapshot_hash="h",
        )
        db.add(combo_version)
        db.flush()
        combo.current_published_version_id = combo_version.id

    with SessionLocal() as db:
        targets = collect_unsupported(db)
        dependents = composite_dependents(db, [v.id for _t, v, _m in targets if v is not None])
        assert [tool.name for tool in dependents] == ["combo_update"]
