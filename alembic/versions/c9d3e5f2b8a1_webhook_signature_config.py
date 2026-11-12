"""Configurable signature headers and algorithms for inbound webhooks."""
from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision = "c9d3e5f2b8a1"
down_revision: str | None = "b8f2c4d1a9e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    sa.Column("signature_header", sa.String(64), nullable=False, server_default="X-Webhook-Signature"),
    sa.Column("timestamp_header", sa.String(64), nullable=False, server_default="X-Webhook-Timestamp"),
    sa.Column("event_id_header", sa.String(64), nullable=False, server_default="X-Webhook-Event-Id"),
    sa.Column("token_header", sa.String(64), nullable=False, server_default="Authorization"),
    sa.Column("token_prefix", sa.String(32), nullable=False, server_default="Bearer "),
    sa.Column("signature_algorithm", sa.String(8), nullable=False, server_default="SHA256"),
    sa.Column("signature_encoding", sa.String(8), nullable=False, server_default="HEX"),
    sa.Column("signature_prefix", sa.String(32), nullable=False, server_default="sha256="),
    sa.Column("signature_template", sa.String(128), nullable=False, server_default="{timestamp}.{body}"),
    sa.Column("timestamp_tolerance_seconds", sa.Integer(), nullable=False, server_default="300"),
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("zr_inbound_webhook", column)


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("zr_inbound_webhook", column.name)
