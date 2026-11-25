"""Align timestamp columns added after the original timezone migration.

The base timezone migration predates several feature tables. These later tables
were created with naive TIMESTAMP columns even though the ORM and runtime use
timezone-aware UTC datetimes. On PostgreSQL interpret existing naive values as
UTC and convert them to TIMESTAMPTZ. SQLite has no timezone-aware storage type.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8f4d2a1b6e9"
down_revision: str | Sequence[str] | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMESTAMP_COLUMNS: dict[str, tuple[str, ...]] = {
    "zr_context_summary": ("created_at", "updated_at"),
    "zr_inbound_webhook": ("created_at", "updated_at", "last_received_at"),
    "zr_inbound_webhook_event": ("created_at", "updated_at"),
    "zr_knowledge_doc": ("created_at", "updated_at", "indexed_at"),
    "zr_knowledge_chunk": ("created_at", "updated_at"),
    "zr_memory_long": ("created_at", "updated_at"),
    "zr_memory_version": ("created_at", "updated_at"),
    "zr_patch_artifact": ("created_at", "updated_at", "expires_at"),
}


def _is_postgresql() -> bool:
    return op.get_context().dialect.name == "postgresql"


def _convert_column(table: str, column: str, *, timezone: bool) -> None:
    target = "TIMESTAMP WITH TIME ZONE" if timezone else "TIMESTAMP WITHOUT TIME ZONE"
    # All columns in this revision were originally created as naive timestamps.
    # Interpret those stored wall-clock values as UTC, not as the DB server's
    # local timezone, to preserve their actual instant.
    op.execute(
        sa.text(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE {target} '
            f'USING "{column}" AT TIME ZONE \'UTC\''
        )
    )


def upgrade() -> None:
    if not _is_postgresql():
        return
    for table, columns in _TIMESTAMP_COLUMNS.items():
        for column in columns:
            _convert_column(table, column, timezone=True)


def downgrade() -> None:
    if not _is_postgresql():
        return
    for table, columns in _TIMESTAMP_COLUMNS.items():
        for column in columns:
            _convert_column(table, column, timezone=False)
