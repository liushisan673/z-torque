"""Inbound webhook media mapping."""
from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: str | None = "f3a4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    sa.Column("media_path", sa.String(128), nullable=True),
    sa.Column("media_type_path", sa.String(64), nullable=False, server_default="type"),
    sa.Column("media_source_path", sa.String(64), nullable=False, server_default="filePath"),
    sa.Column("media_mime_path", sa.String(64), nullable=False, server_default="mimeType"),
    sa.Column("media_file_name_path", sa.String(64), nullable=False, server_default="file_name"),
    sa.Column("media_placeholder_texts", sa.JSON(), nullable=True),
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("zr_inbound_webhook", column)


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("zr_inbound_webhook", column.name)
