"""内核向插件开放的只读投影（标准插件可移植性缝，v1.3 §6.2）。

背景：标准插件不得 `from app.models import ...`（否则搬到企业版必然因模型差异炸掉）。
插件需要的**内核自有表**只读视角全部收敛到本模块，由 `PluginSdk` 暴露：

- `list_published_agents`：已发布 Agent 的能力投影（a2a_gateway 本地发现用）；
- `list_tenant_ids`：租户枚举（audit_export_archive 定时归档用）。

约定：
- 这里的 SQL 属于内核，插件拿不到 ORM 类，也无法改写投影字段；
- 一律只读，写路径仍由内核服务负责；
- 返回纯 dict / 标量，调用方会话关闭后仍然可用。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.models import Agent, AgentVersion, Tenant

# 允许对外暴露的 capability 键（其余 config_snapshot 内容一律不投影）
CAPABILITY_KEYS = ("capabilities", "skills", "tags", "domains", "a2a")

MAX_DISCOVERY_LIMIT = 200


def extract_capabilities(snapshot: Any) -> list[str]:
    """从版本快照中抽取 capability 白名单键（去重 + 稳定排序）。"""
    if not isinstance(snapshot, dict):
        return []
    found: list[str] = []
    for key in CAPABILITY_KEYS:
        value = snapshot.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
        elif isinstance(value, list):
            found.extend(str(item).strip() for item in value if str(item).strip())
    # 去重且稳定排序：发现结果是外部契约，不能因 dict 顺序抖动
    return sorted(dict.fromkeys(found))


def list_published_agents(
    session_factory: Callable[[], AbstractContextManager[Any]],
    *,
    tenant_id: UUID,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """租户内**已发布** Agent 的只读投影（不暴露草稿与提示词全文）。"""
    bounded = max(1, min(int(limit), MAX_DISCOVERY_LIMIT))
    with session_factory() as db:
        rows = db.execute(
            select(Agent, AgentVersion)
            .join(AgentVersion, AgentVersion.id == Agent.current_published_version_id)
            .where(
                Agent.tenant_id == tenant_id,
                Agent.status == "ACTIVE",
                AgentVersion.tenant_id == tenant_id,
                AgentVersion.status == "PUBLISHED",
            )
            .order_by(Agent.name.asc())
            .limit(bounded)
        ).all()
        # 会话关闭前完成投影（避免 ORM 实例脱离会话后属性失效）
        return [
            {
                "agent_id": str(agent.id),
                "agent_version_id": str(version.id),
                "name": agent.name,
                "description": agent.description or "",
                "version": version.version_number,
                "capabilities": extract_capabilities(version.config_snapshot),
            }
            for agent, version in rows
        ]


def list_tenant_ids(session_factory: Callable[[], AbstractContextManager[Any]]) -> list[UUID]:
    """全部租户 ID（定时归档按租户逐个触发；只读、无过滤语义）。"""
    with session_factory() as db:
        return [item.id for item in db.scalars(select(Tenant)).all()]
