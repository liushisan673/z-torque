"""Optional reply-back configuration for inbound webhooks."""
from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision = "f3a4b5c6d7e8"
down_revision: str | None = "c9d3e5f2b8a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    sa.Column("reply_url", sa.String(512), nullable=True),
    sa.Column("reply_token_ciphertext", sa.String(1024), nullable=True),
    sa.Column("reply_account_path", sa.String(128), nullable=False, server_default="account_id"),
    sa.Column("reply_peer_path", sa.String(128), nullable=False, server_default="peer_id"),
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("zr_inbound_webhook", column)


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("zr_inbound_webhook", column.name)
