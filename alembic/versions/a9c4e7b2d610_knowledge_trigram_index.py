"""Rebuild the local knowledge index with FTS5 trigram tokenization.

Chinese text is commonly stored without spaces. unicode61 treats a contiguous Chinese
sentence as one token, so phrase fragments such as "知识库" do not match. The trigram
tokenizer enables substring retrieval while retaining FTS5 BM25 ranking. Databases
without SQLite FTS5 trigram use a compatible ordinary table and LIKE fallback.

Revision ID: a9c4e7b2d610
Revises: f9a7b3c5d182
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9c4e7b2d610"
down_revision: str | None = "f9a7b3c5d182"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FTS = "zr_knowledge_fts"


def _trigram_available() -> bool:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return False
    try:
        connection.exec_driver_sql(
            "CREATE VIRTUAL TABLE _zt_fts_probe USING fts5(text, tokenize='trigram')"
        )
        connection.exec_driver_sql("DROP TABLE _zt_fts_probe")
        return True
    except Exception:  # noqa: BLE001 - unsupported tokenizer uses the ordinary-table fallback
        return False


def _create_fallback_table() -> None:
    # Explicit rowid keeps deletion semantics portable across SQLite and PostgreSQL.
    op.create_table(
        _FTS,
        sa.Column("rowid", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("chunk_id", sa.String(36), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
    )
    op.create_index(f"ix_{_FTS}_tenant_id", _FTS, ["tenant_id"])


def _create_index() -> None:
    if _trigram_available():
        op.execute(
            f"CREATE VIRTUAL TABLE {_FTS} USING fts5(tenant_id UNINDEXED, chunk_id UNINDEXED, text, tokenize='trigram')"
        )
    else:
        _create_fallback_table()
    # FTS5 and the fallback table both allocate their own rowid. The chunk_id is
    # the stable cross-dialect key; execution code resolves the index rowid by it.
    op.execute(
        f"INSERT INTO {_FTS}(tenant_id, chunk_id, text) "
        "SELECT CAST(c.tenant_id AS VARCHAR(36)), CAST(c.id AS VARCHAR(36)), c.text FROM zr_knowledge_chunk c"
    )


def upgrade() -> None:
    op.drop_table(_FTS)
    _create_index()


def downgrade() -> None:
    op.drop_table(_FTS)
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        try:
            connection.exec_driver_sql(
                "CREATE VIRTUAL TABLE _zt_fts_probe USING fts5(text, tokenize='unicode61')"
            )
            connection.exec_driver_sql("DROP TABLE _zt_fts_probe")
            op.execute(
                f"CREATE VIRTUAL TABLE {_FTS} USING fts5(tenant_id UNINDEXED, chunk_id UNINDEXED, text, tokenize='unicode61')"
            )
        except Exception:  # noqa: BLE001 - fall back when FTS5 is unavailable
            _create_fallback_table()
    else:
        _create_fallback_table()
    op.execute(
        f"INSERT INTO {_FTS}(tenant_id, chunk_id, text) "
        "SELECT c.tenant_id, c.id, c.text FROM zr_knowledge_chunk c"
    )
