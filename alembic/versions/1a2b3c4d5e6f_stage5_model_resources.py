"""Stage 5 model provider, model resource and endpoint schema."""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1a2b3c4d5e6f"
down_revision: str | None = "f9a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "zr_model_provider",
        sa.Column("id", sa.CHAR(36), primary_key=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("provider_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ENABLED"),
        sa.Column("base_url", sa.String(512), nullable=True),
        sa.Column("secret_ref", sa.String(512), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_by", sa.CHAR(36), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["zr_user.id"]),
        sa.UniqueConstraint("tenant_id", "name", name="uq_zr_model_provider_tenant_name"),
    )
    op.create_index("ix_zr_model_provider_tenant_id", "zr_model_provider", ["tenant_id"])
    op.create_index("ix_zr_model_provider_status", "zr_model_provider", ["status"])

    op.create_table(
        "zr_model",
        sa.Column("id", sa.CHAR(36), primary_key=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_id", sa.CHAR(36), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("model_identifier", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ENABLED"),
        sa.Column("supports_streaming", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("supports_function_calling", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.ForeignKeyConstraint(["provider_id"], ["zr_model_provider.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_zr_model_tenant_id", "zr_model", ["tenant_id"])
    op.create_index("ix_zr_model_provider_id", "zr_model", ["provider_id"])
    op.create_index("ix_zr_model_status", "zr_model", ["status"])

    op.create_table(
        "zr_model_endpoint",
        sa.Column("id", sa.CHAR(36), primary_key=True),
        sa.Column("tenant_id", sa.CHAR(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_id", sa.CHAR(36), nullable=False),
        sa.Column("model_id", sa.CHAR(36), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("endpoint_url", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ENABLED"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.ForeignKeyConstraint(["provider_id"], ["zr_model_provider.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["model_id"], ["zr_model.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_zr_model_endpoint_tenant_id", "zr_model_endpoint", ["tenant_id"])
    op.create_index("ix_zr_model_endpoint_provider_id", "zr_model_endpoint", ["provider_id"])
    op.create_index("ix_zr_model_endpoint_model_id", "zr_model_endpoint", ["model_id"])
    op.create_index("ix_zr_model_endpoint_status", "zr_model_endpoint", ["status"])


def downgrade() -> None:
    op.drop_index("ix_zr_model_endpoint_status", table_name="zr_model_endpoint")
    op.drop_index("ix_zr_model_endpoint_model_id", table_name="zr_model_endpoint")
    op.drop_index("ix_zr_model_endpoint_provider_id", table_name="zr_model_endpoint")
    op.drop_index("ix_zr_model_endpoint_tenant_id", table_name="zr_model_endpoint")
    op.drop_table("zr_model_endpoint")
    op.drop_index("ix_zr_model_status", table_name="zr_model")
    op.drop_index("ix_zr_model_provider_id", table_name="zr_model")
    op.drop_index("ix_zr_model_tenant_id", table_name="zr_model")
    op.drop_table("zr_model")
    op.drop_index("ix_zr_model_provider_status", table_name="zr_model_provider")
    op.drop_index("ix_zr_model_provider_tenant_id", table_name="zr_model_provider")
    op.drop_table("zr_model_provider")
