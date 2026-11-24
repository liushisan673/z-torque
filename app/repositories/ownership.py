"""属主过滤收口层（Phase 2 §四第 2 项）。

单机固定 tenant_id，但同一租户内仍有用户 / 智能体边界。所有按 id 取资源的
服务必须经本模块，禁止裸 `db.get(Model, id)` 绕过属主判定。

可见性口径与既有 `resolve_conversation_agent` 保持一致：
本人资源 OR 平台资源（OFFICIAL / official=True）OR 管理员创建的资源。
这里只做单机内的跨用户 / 跨 agent 越权防护，不设计跨租户查询矩阵。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.security.permissions import admin_owned_clause


def visible_owner_clause(model: Any, tenant_id: UUID, user_id: UUID):
    """平台资源对同租户用户可见的 SQL 条件（本人 / 官方 / 管理员创建）。"""
    clauses = [model.owner_user_id == user_id]
    if hasattr(model, "source_type"):
        clauses.append(model.source_type == "OFFICIAL")
    if hasattr(model, "official"):
        clauses.append(model.official.is_(True))
    clauses.append(admin_owned_clause(model.owner_user_id, tenant_id))
    return or_(*clauses)


def is_visible(db: Session, model: Any, *, resource_id: Any, tenant_id: UUID, user_id: UUID) -> bool:
    """资源是否对当前用户可见（用于目录过滤：不可见则跳过，不抛异常）。"""
    if resource_id is None:
        return False
    return db.scalar(
        select(model.id).where(
            model.id == resource_id,
            model.tenant_id == tenant_id,
            visible_owner_clause(model, tenant_id, user_id),
        )
    ) is not None


def get_owned(
    db: Session,
    model: Any,
    *,
    resource_id: Any,
    tenant_id: UUID,
    user_id: UUID | None,
    error_code: str = "AI_RESOURCE_NOT_FOUND",
    label: str = "resource",
) -> Any:
    """取属主可见资源；不可见或不存在一律 404（不泄露资源是否存在）。"""
    if resource_id is None:
        raise ApiError(error_code, f"{label} not found", 404)
    query = select(model).where(model.id == resource_id, model.tenant_id == tenant_id)
    if user_id is not None:
        query = query.where(visible_owner_clause(model, tenant_id, user_id))
    obj = db.scalar(query)
    if obj is None:
        raise ApiError(error_code, f"{label} not found", 404)
    return obj


def get_owned_version(
    db: Session,
    version_model: Any,
    parent_model: Any,
    parent_fk: str,
    *,
    version_id: Any,
    tenant_id: UUID,
    user_id: UUID | None,
    error_code: str = "AI_RESOURCE_NOT_FOUND",
    label: str = "version",
) -> Any:
    """版本表无 owner 列：先按 tenant 取版本，再按父资源属主校验。"""
    if version_id is None:
        raise ApiError(error_code, f"{label} not found", 404)
    version = db.scalar(
        select(version_model).where(version_model.id == version_id, version_model.tenant_id == tenant_id)
    )
    if version is None:
        raise ApiError(error_code, f"{label} not found", 404)
    get_owned(
        db,
        parent_model,
        resource_id=getattr(version, parent_fk),
        tenant_id=tenant_id,
        user_id=user_id,
        error_code=error_code,
        label=label,
    )
    return version
