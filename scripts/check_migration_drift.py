"""打印「数据库实际结构」与「ORM 模型」的差异明细。

`alembic check` 只给一行结论，排查漂移时看不到全貌。这个脚本用与迁移环境
完全相同的比较策略（`app.db.alembic_support.compare_type`）输出逐条差异，
便于在干净库上判断「迁移链是否真的能建出模型期望的结构」。

用法：
    python scripts/check_migration_drift.py                       # 取 DATABASE_URL
    python scripts/check_migration_drift.py postgresql+pg8000://user:pass@host/db

退出码：无差异 0，有差异 1。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.runtime.migration import MigrationContext  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

# 插件私有表（方案 b）：模型定义跟随插件目录，必须显式 import 才能进入 Base.metadata。
# 漏掉这一行时，DB 里已由迁移链建好的三张表会被判成「多余的表，应 DROP」（total != 0 的假阳性）。
import app.builtin_plugins.audit_export_archive.models  # noqa: E402, F401
import app.models  # noqa: E402, F401
from app.config import get_settings  # noqa: E402
from app.db.alembic_support import compare_type  # noqa: E402
from app.db.base import Base  # noqa: E402


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else get_settings().database_url
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"compare_type": compare_type})
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    for entry in diff:
        print(entry)
    print(f"total={len(diff)}")
    return 0 if not diff else 1


if __name__ == "__main__":
    raise SystemExit(main())
