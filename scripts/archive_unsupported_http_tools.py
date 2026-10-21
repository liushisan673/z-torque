"""一次性运维：归档「方法不可执行」的存量 HTTP 工具。

背景
----
注册入口已收口为仅允许 GET/POST（与 HttpExecutor 白名单同源），但历史上通过
OpenAPI 导入的 PUT/PATCH/DELETE 工具仍在库中且为 ACTIVE。这些工具一旦被技能绑定，
模型会看到「可调用」，调用却必然返回 AI_HTTP_METHOD_NOT_ALLOWED——每次浪费一轮
模型调用，并产生失败审计噪音（死工具）。

处理口径
--------
- 只把 ``Tool.status`` 置为 ARCHIVED，**不删除**任何行：ToolVersion、ConnectorTool、
  SkillVersionTool 绑定与历史审计全部保留，可追溯、可人工复核；
- 只处理 ``tool_type='HTTP'`` 且当前发布版本 ``config_snapshot.method`` 不在
  GET/POST 的 ACTIVE 工具；MCP（tools/call）、DB（SQL 校验）、COMPOSITE 无方法概念，不受影响；
- 幂等：重复执行只会命中仍为 ACTIVE 的记录，第二次即 0 条；
- 默认 dry-run，加 ``--apply`` 才写库；每条归档写入 TOOL_ARCHIVED 审计（actor 为空）。

用法
----
    python scripts/archive_unsupported_http_tools.py                    # 预览
    python scripts/archive_unsupported_http_tools.py --apply            # 执行
    python scripts/archive_unsupported_http_tools.py --tenant <uuid> --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.builtin_plugins.executors import SUPPORTED_HTTP_METHODS
from app.db.session import SessionLocal
from app.kernel.audit.service import record_audit
from app.models import Tool, ToolVersion


def tool_method(tool: Tool, version: ToolVersion | None) -> str:
    """工具实际请求方法；缺省与执行器保持一致（GET）。"""
    snapshot = version.config_snapshot if version is not None and isinstance(version.config_snapshot, dict) else {}
    return str(snapshot.get("method") or "GET").upper()


def collect_unsupported(db: Session) -> list[tuple[Tool, ToolVersion | None, str]]:
    rows = db.execute(
        select(Tool, ToolVersion)
        .outerjoin(ToolVersion, ToolVersion.id == Tool.current_published_version_id)
        .where(Tool.tool_type == "HTTP", Tool.status == "ACTIVE")
        .order_by(Tool.created_at)
    ).all()
    targets: list[tuple[Tool, ToolVersion | None, str]] = []
    for tool, version in rows:
        method = tool_method(tool, version)
        if method not in SUPPORTED_HTTP_METHODS:
            targets.append((tool, version, method))
    return targets


def composite_dependents(db: Session, dead_version_ids: list[UUID]) -> list[Tool]:
    """引用了待归档版本的 ACTIVE 组合工具（子工具不可执行 → 组合工具整体必然失败）。"""
    dead = {str(v) for v in dead_version_ids}
    if not dead:
        return []
    hits: list[Tool] = []
    rows = db.execute(
        select(Tool, ToolVersion)
        .outerjoin(ToolVersion, ToolVersion.id == Tool.current_published_version_id)
        .where(Tool.tool_type == "COMPOSITE", Tool.status == "ACTIVE")
    ).all()
    for tool, version in rows:
        snapshot = version.config_snapshot if version is not None and isinstance(version.config_snapshot, dict) else {}
        steps = snapshot.get("steps") if isinstance(snapshot.get("steps"), list) else []
        if any(isinstance(step, dict) and str(step.get("tool_version_id")) in dead for step in steps):
            hits.append(tool)
    return hits


def archive(db: Session, targets: list[tuple[Tool, ToolVersion | None, str]]) -> int:
    for tool, version, method in targets:
        tool.status = "ARCHIVED"
        record_audit(
            db,
            tenant_id=tool.tenant_id,
            actor_user_id=None,
            action="TOOL_ARCHIVED",
            resource_type="TOOL",
            resource_id=tool.id,
            summary="archive tool with unsupported http method",
            detail={
                "source": "one_off_migration",
                "reason": "method not supported",
                "method": method,
                "tool_version_id": str(version.id) if version is not None else None,
            },
        )
    db.commit()
    return len(targets)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="归档方法不受支持的存量 HTTP 工具（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="实际写库；缺省只打印预览")
    parser.add_argument("--tenant", help="只处理指定租户 UUID")
    args = parser.parse_args(argv)

    tenant_id: UUID | None = None
    if args.tenant:
        try:
            tenant_id = UUID(args.tenant)
        except ValueError:
            print(f"invalid tenant uuid: {args.tenant}")
            return 2

    with SessionLocal() as db:
        targets = collect_unsupported(db)
        if tenant_id is not None:
            targets = [item for item in targets if item[0].tenant_id == tenant_id]

        print(f"supported methods: {'/'.join(SUPPORTED_HTTP_METHODS)}")
        print(f"found {len(targets)} ACTIVE HTTP tool(s) with unsupported method")
        for tool, version, method in targets:
            print(
                f"  [{method}] {tool.name}  tool={tool.id}  "
                f"version={version.id if version is not None else '-'}  tenant={tool.tenant_id}"
            )

        dependents = composite_dependents(db, [v.id for _t, v, _m in targets if v is not None])
        if dependents:
            print(f"WARNING: {len(dependents)} ACTIVE composite tool(s) reference them and will still fail at execution:")
            for tool in dependents:
                print(f"  COMPOSITE {tool.name}  tool={tool.id}")

        if not targets:
            print("nothing to do")
            return 0
        if not args.apply:
            print("dry-run only; rerun with --apply to archive")
            return 0

        count = archive(db, targets)
        print(f"archived {count} tool(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
