"""Generic inbound webhooks for external system callbacks."""
from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision = "b8f2c4d1a9e7"
down_revision: str | None = "c7a1e4b90d23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    common = [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]
    op.create_table(
        "zr_inbound_webhook",
        *common,
        sa.Column("owner_user_id", sa.String(36), sa.ForeignKey("zr_user.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("secret_ciphertext", sa.String(1024), nullable=False),
        sa.Column("auth_mode", sa.String(8), nullable=False, server_default="HMAC"),
        sa.Column("target_agent_id", sa.String(36), sa.ForeignKey("zr_agent.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("message_path", sa.String(128), nullable=False, server_default="text"),
        sa.Column("conversation_key_path", sa.String(128), nullable=False, server_default="conversation_key"),
        sa.Column("event_id_path", sa.String(128), nullable=False, server_default="event_id"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_received_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_zr_inbound_webhook_tenant_id", "zr_inbound_webhook", ["tenant_id"])
    op.create_index("ix_zr_inbound_webhook_owner_user_id", "zr_inbound_webhook", ["owner_user_id"])
    op.create_index("ix_zr_inbound_webhook_target_agent_id", "zr_inbound_webhook", ["target_agent_id"])

    op.create_table(
        "zr_inbound_webhook_event",
        *common,
        sa.Column("webhook_id", sa.String(36), sa.ForeignKey("zr_inbound_webhook.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACCEPTED"),
        sa.Column("error", sa.String(512), nullable=True),
        sa.UniqueConstraint("webhook_id", "event_id", name="uq_zr_inbound_webhook_event"),
    )
    op.create_index("ix_zr_inbound_webhook_event_tenant_id", "zr_inbound_webhook_event", ["tenant_id"])
    op.create_index("ix_zr_inbound_webhook_event_webhook_id", "zr_inbound_webhook_event", ["webhook_id"])


def downgrade() -> None:
    op.drop_index("ix_zr_inbound_webhook_event_webhook_id", table_name="zr_inbound_webhook_event")
    op.drop_index("ix_zr_inbound_webhook_event_tenant_id", table_name="zr_inbound_webhook_event")
    op.drop_table("zr_inbound_webhook_event")
    op.drop_index("ix_zr_inbound_webhook_target_agent_id", table_name="zr_inbound_webhook")
    op.drop_index("ix_zr_inbound_webhook_owner_user_id", table_name="zr_inbound_webhook")
    op.drop_index("ix_zr_inbound_webhook_tenant_id", table_name="zr_inbound_webhook")
    op.drop_table("zr_inbound_webhook")
