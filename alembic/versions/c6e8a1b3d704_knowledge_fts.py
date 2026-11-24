"""Add local knowledge docs/chunks + SQLite FTS5 index with LIKE fallback (Phase 0 §2.1).

FTS 索引由应用侧双写（不依赖 external-content 触发器）；目标 Python 的 sqlite3
不支持 FTS5 时建同构普通表 + LIKE 索引，检索侧上报 DEGRADED 并写审计。
tenant_id 保留但单机固定值。

Revision ID: c6e8a1b3d704
Revises: b4d8f0a2c913
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c6e8a1b3d704"
down_revision: str | None = "b4d8f0a2c913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DOC = "zr_knowledge_doc"
_CHUNK = "zr_knowledge_chunk"
_FTS = "zr_knowledge_fts"


def _fts5_available() -> bool:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return False
    try:
        connection.exec_driver_sql("CREATE VIRTUAL TABLE _zt_fts_probe USING fts5(text)")
        connection.exec_driver_sql("DROP TABLE _zt_fts_probe")
    except Exception:  # noqa: BLE001 - 探测失败即降级，不假设 FTS5 存在
        return False
    return True


def upgrade() -> None:
    op.create_table(
        _DOC,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("file_id", sa.String(36), nullable=True),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column("truncated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("indexed_at", sa.DateTime(), nullable=True),
    )
    for column in ("tenant_id", "file_id", "path", "status"):
        op.create_index(f"ix_{_DOC}_{column}", _DOC, [column])

    op.create_table(
        _CHUNK,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("doc_id", sa.String(36), sa.ForeignKey(f"{_DOC}.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=True),
        sa.Column("sheet", sa.String(128), nullable=True),
        sa.Column("row_no", sa.Integer(), nullable=True),
        sa.Column("col_no", sa.Integer(), nullable=True),
        sa.Column("line_start", sa.Integer(), nullable=True),
        sa.Column("line_end", sa.Integer(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
    )
    for column in ("tenant_id", "doc_id"):
        op.create_index(f"ix_{_CHUNK}_{column}", _CHUNK, [column])
    op.create_index(f"ix_{_CHUNK}_doc_seq", _CHUNK, ["doc_id", "seq"], unique=True)

    # 应用侧双写：FTS 表独立于 chunk 表，避免 external-content 触发器在
    # 既有库/测试 create_all 路径下漏建导致索引空白。两种形态列集合一致。
    if _fts5_available():
        op.execute(
            f"CREATE VIRTUAL TABLE {_FTS} USING fts5(chunk_id, text, tokenize='unicode61')"
        )
    else:
        op.create_table(
            _FTS,
            sa.Column("chunk_id", sa.String(36), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
        )
        op.create_index(f"ix_{_FTS}_text", _FTS, ["text"])


def downgrade() -> None:
    op.drop_table(_FTS)
    op.drop_index(f"ix_{_CHUNK}_doc_seq", table_name=_CHUNK)
    for column in ("tenant_id", "doc_id"):
        op.drop_index(f"ix_{_CHUNK}_{column}", table_name=_CHUNK)
    op.drop_table(_CHUNK)
    for column in ("tenant_id", "file_id", "path", "status"):
        op.drop_index(f"ix_{_DOC}_{column}", table_name=_DOC)
    op.drop_table(_DOC)
