"""Stage 07 connector and tool provenance schema."""
from typing import Sequence
from alembic import op
import sqlalchemy as sa

revision = "e7f8a9b0c1d2"
down_revision: str | None = "c7f4a2b91d30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    common = [sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant_id", sa.String(36), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False)]
    op.create_table("zr_connector", *common, sa.Column("name", sa.String(128), nullable=False), sa.Column("connector_type", sa.String(16), nullable=False), sa.Column("status", sa.String(16), nullable=False), sa.Column("config", sa.JSON(), nullable=False), sa.Column("secret_ref", sa.String(512)), sa.Column("target_policy", sa.JSON(), nullable=False), sa.Column("created_by", sa.String(36), nullable=False), sa.Column("description", sa.Text()))
    op.create_table("zr_connector_tool", *common, sa.Column("connector_id", sa.String(36), sa.ForeignKey("zr_connector.id", ondelete="RESTRICT"), nullable=False), sa.Column("tool_id", sa.String(36), sa.ForeignKey("zr_tool.id", ondelete="RESTRICT"), nullable=False), sa.Column("external_name", sa.String(128)), sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_table("zr_connector_tool")
    op.drop_table("zr_connector")
