"""审计只读缝（app.kernel.audit.reader）专项单测（v1.3 §3.7 / B-12）。

覆盖四类此前只有代码审查、没有用例的行为：
1. keyset 分页在 (created_at, id) 复合游标下**不重不漏**（含同一时刻多行）；
2. 租户隔离：任何过滤组合都不得跨租户读取；
3. `actions=()` 与 `actions=None` 语义不可混淆（空元组 = 无匹配）；
4. 会话外投影安全：yield 出去的是稳定 dataclass，不是已脱离会话的 ORM 实例。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from app.db.session import SessionLocal
from app.kernel.audit.reader import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    AuditQuery,
    AuditRecord,
    iter_audit_events,
)
from app.models import AuditLog
from tests.support.fakes import FakeSdk

_BASE = datetime(2026, 3, 1, 12, 0, 0)


def _factory():
    """与真实 SDK 一致：每次迭代开一个短会话（不复用请求会话）。"""
    return FakeSdk(read_audit=True, permissions=("db.query", "audit.read")).session


def _seed(rows: list[AuditLog]) -> list[AuditLog]:
    with SessionLocal.begin() as db:
        for row in rows:
            db.add(row)
        db.flush()
        for row in rows:
            db.refresh(row)
    return rows


def _make_row(*, tenant_id, action="AGENT_CREATED", created_at=None, actor_user_id=None, resource_type="AGENT", detail=None, summary="s"):
    return AuditLog(
        tenant_id=tenant_id,
        action=action,
        resource_type=resource_type,
        resource_id=None,
        actor_user_id=actor_user_id,
        summary=summary,
        detail=detail,
        created_at=created_at or _BASE,
    )


def test_keyset_pagination_is_lossless_and_duplicate_free():
    """7 行 / 每页 2 行：跨越 4 次分页，含同一 created_at 的并列行。"""
    tenant_id = uuid4()
    # 同一时刻 3 行（触发 and_(created_at == cursor, id > cursor_id) 分支）
    rows = [
        _make_row(tenant_id=tenant_id, created_at=_BASE),
        _make_row(tenant_id=tenant_id, created_at=_BASE),
        _make_row(tenant_id=tenant_id, created_at=_BASE),
        _make_row(tenant_id=tenant_id, created_at=_BASE + timedelta(seconds=1)),
        _make_row(tenant_id=tenant_id, created_at=_BASE + timedelta(seconds=2)),
        _make_row(tenant_id=tenant_id, created_at=_BASE + timedelta(seconds=2)),
        _make_row(tenant_id=tenant_id, created_at=_BASE + timedelta(seconds=3)),
    ]
    _seed(rows)

    records = list(iter_audit_events(_factory(), AuditQuery(tenant_id=tenant_id, page_size=2)))

    assert len(records) == len(rows), "keyset 分页不得漏行"
    ids = [record.id for record in records]
    assert len(set(ids)) == len(ids), "keyset 分页不得重复产出同一行"
    expected = [
        str(row.id)
        for row in sorted(rows, key=lambda item: (item.created_at, str(item.id)))
    ]
    assert ids == expected, "必须严格按 (created_at, id) 升序且游标不跳行"


def test_page_size_one_does_not_skip_or_repeat():
    tenant_id = uuid4()
    rows = [_make_row(tenant_id=tenant_id, created_at=_BASE + timedelta(seconds=index)) for index in range(5)]
    _seed(rows)

    records = list(iter_audit_events(_factory(), AuditQuery(tenant_id=tenant_id, page_size=1)))

    assert [record.created_at for record in records] == [row.created_at for row in rows]


def test_tenant_isolation_always_applies():
    tenant_a, tenant_b = uuid4(), uuid4()
    _seed(
        [
            _make_row(tenant_id=tenant_a, action="AGENT_CREATED"),
            _make_row(tenant_id=tenant_a, action="SKILL_CREATED"),
            _make_row(tenant_id=tenant_b, action="AGENT_CREATED"),
            _make_row(tenant_id=tenant_b, action="SKILL_CREATED"),
        ]
    )

    records = list(iter_audit_events(_factory(), AuditQuery(tenant_id=tenant_a)))

    assert {record.tenant_id for record in records} == {str(tenant_a)}
    assert len(records) == 2


def test_empty_actions_tuple_means_no_match_not_all():
    """`actions=()` 必须生成恒假条件；`actions=None` 才是不限动作。"""
    tenant_id = uuid4()
    _seed([_make_row(tenant_id=tenant_id, action="AGENT_CREATED")])

    assert list(iter_audit_events(_factory(), AuditQuery(tenant_id=tenant_id, actions=()))) == []
    assert len(list(iter_audit_events(_factory(), AuditQuery(tenant_id=tenant_id, actions=None)))) == 1


def _seed_actor(tenant_id) -> tuple:
    """审计 actor_user_id 带外键，必须落真实租户/用户（也顺便验证跨租户读不到）。"""
    from app.models import Tenant, User

    with SessionLocal.begin() as db:
        if db.scalars(select(Tenant).where(Tenant.id == tenant_id)).one_or_none() is None:
            db.add(Tenant(id=tenant_id, name=f"t-{tenant_id}"))
            db.flush()
        actor = User(tenant_id=tenant_id, username=f"actor-{uuid4()}", password_hash="x", display_name="actor")
        other = User(tenant_id=tenant_id, username=f"actor-{uuid4()}", password_hash="x", display_name="other")
        db.add_all([actor, other])
        db.flush()
        return str(actor.id), str(other.id)


def test_filters_are_composable_without_widening_scope():
    tenant_id = uuid4()
    actor, other_actor = _seed_actor(tenant_id)
    _seed(
        [
            _make_row(tenant_id=tenant_id, action="SKILL_EXECUTED", actor_user_id=actor, resource_type="SKILL", created_at=_BASE),
            _make_row(tenant_id=tenant_id, action="SKILL_EXECUTED", actor_user_id=other_actor, resource_type="SKILL", created_at=_BASE + timedelta(hours=1)),
            _make_row(tenant_id=tenant_id, action="FILE_READ", actor_user_id=actor, resource_type="FILE", created_at=_BASE + timedelta(hours=2)),
            _make_row(tenant_id=tenant_id, action="SKILL_EXECUTED", actor_user_id=actor, resource_type="AGENT", created_at=_BASE + timedelta(hours=3)),
        ]
    )

    combined = list(
        iter_audit_events(
            _factory(),
            AuditQuery(
                tenant_id=tenant_id,
                actions=("SKILL_EXECUTED",),
                actor_user_id=actor,
                resource_type="SKILL",
                start_time=_BASE - timedelta(seconds=1),
                end_time=_BASE + timedelta(minutes=30),
            ),
        )
    )

    assert [record.action for record in combined] == ["SKILL_EXECUTED"]
    assert [record.actor_user_id for record in combined] == [str(actor)]

    prefixed = list(iter_audit_events(_factory(), AuditQuery(tenant_id=tenant_id, action_prefix="SKILL_")))
    assert {record.action for record in prefixed} == {"SKILL_EXECUTED"}
    assert len(prefixed) == 3


def test_records_are_detached_projections_not_orm_instances():
    tenant_id = uuid4()
    [row] = _seed([_make_row(tenant_id=tenant_id, detail={"api_key": "plain-text", "nested": {"keep": 1}})])

    records = list(iter_audit_events(_factory(), AuditQuery(tenant_id=tenant_id)))

    assert len(records) == 1
    record = records[0]
    assert isinstance(record, AuditRecord)
    assert not hasattr(record, "_sa_instance_state"), "不得把 ORM 实例泄漏给插件"
    assert isinstance(record.id, str) and isinstance(record.tenant_id, str)
    # 会话已关闭仍可读（属性在会话内完成投影）
    assert record.summary == "s"
    assert record.category == "config"
    # 修改投影不得回写数据库（无会话、无脏检查）
    record.detail["api_key"] = "tampered"
    with SessionLocal() as db:
        stored = db.scalars(select(AuditLog).where(AuditLog.id == row.id)).one()
        assert stored.detail == {"api_key": "plain-text", "nested": {"keep": 1}}


def test_non_mapping_detail_is_projected_as_none():
    """detail 列为非 dict（历史脏数据）时必须降级为 None，不得让导出层崩在属性访问上。"""
    tenant_id = uuid4()
    _seed([_make_row(tenant_id=tenant_id, detail=["not", "a", "mapping"])])

    [record] = list(iter_audit_events(_factory(), AuditQuery(tenant_id=tenant_id)))

    assert record.detail is None


def test_unknown_action_falls_back_to_default_category():
    tenant_id = uuid4()
    _seed([_make_row(tenant_id=tenant_id, action="SOMETHING_NOT_REGISTERED")])

    [record] = list(iter_audit_events(_factory(), AuditQuery(tenant_id=tenant_id)))

    assert record.category == "config"


def test_page_size_is_clamped_into_supported_range():
    tenant_id = uuid4()

    assert AuditQuery(tenant_id=tenant_id, page_size=10_000).normalized_page_size() == MAX_PAGE_SIZE
    assert AuditQuery(tenant_id=tenant_id, page_size=0).normalized_page_size() == DEFAULT_PAGE_SIZE
    assert AuditQuery(tenant_id=tenant_id, page_size=7).normalized_page_size() == 7


def test_plugin_without_audit_read_declaration_is_rejected():
    """read_audit 是显式能力：未声明时 SDK 必须拒绝（不能靠"插件是内置的"放行）。"""
    tenant_id = uuid4()
    sdk = FakeSdk(read_audit=False)

    try:
        sdk.read_audit(AuditQuery(tenant_id=tenant_id))
    except PermissionError as exc:
        assert "audit read" in str(exc)
    else:  # pragma: no cover - 声明能力缺失必须失败
        raise AssertionError("read_audit must be denied without manifest.read_audit")
