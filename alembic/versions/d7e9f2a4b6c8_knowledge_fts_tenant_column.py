"""Add tenant_id column to zr_knowledge_fts (Phase 0 §2.1 / §八 收口).

路线图 §八 要求 doc/chunk/fts 三张表全部带 tenant_id（单机固定值）。
FTS5 虚表不支持 ALTER ADD COLUMN，故重建并回填；不支持 FTS5 的库重建为
同构普通表，保证两种形态列集合一致。

Revision ID: d7e9f2a4b6c8
Revises: c6e8a1b3d704
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d7e9f2a4b6c8"
down_revision: str | None = "c6e8a1b3d704"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FTS = "zr_knowledge_fts"


def _fts5_available() -> bool:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return False
    try:
        connection.exec_driver_sql("CREATE VIRTUAL TABLE _zt_fts_probe USING fts5(text)")
        connection.exec_driver_sql("DROP TABLE _zt_fts_probe")
        return True
    except Exception:  # noqa: BLE001 - 不支持 FTS5 时走同构普通表
        return False


def _rebuild(*, with_tenant: bool) -> None:
    op.drop_table(_FTS)
    if _fts5_available():
        columns = "tenant_id, " if with_tenant else ""
        op.execute(f"CREATE VIRTUAL TABLE {_FTS} USING fts5({columns}chunk_id, text, tokenize='unicode61')")
    else:
        columns = [sa.Column("rowid", sa.Integer(), primary_key=True, autoincrement=True)]
        if with_tenant:
            columns.append(sa.Column("tenant_id", sa.String(36), nullable=False))
        columns.extend([
            sa.Column("chunk_id", sa.String(36), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
        ])
        op.create_table(_FTS, *columns)
        op.create_index(f"ix_{_FTS}_text", _FTS, ["text"])
    target_columns = "tenant_id, chunk_id, text" if with_tenant else "chunk_id, text"
    source_columns = "c.tenant_id, c.id, c.text" if with_tenant else "c.id, c.text"
    op.execute(f"INSERT INTO {_FTS}({target_columns}) SELECT {source_columns} FROM zr_knowledge_chunk c")


def upgrade() -> None:
    _rebuild(with_tenant=True)


def downgrade() -> None:
    _rebuild(with_tenant=False)
