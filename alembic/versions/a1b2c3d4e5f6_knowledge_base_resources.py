"""Add user-owned knowledge bases and file membership.

Revision ID: a1b2c3d4e5f6
Revises: f9a7b3c5d182
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "f9a7b3c5d182"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "zr_knowledge_base",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_user_id", sa.String(36), sa.ForeignKey("zr_user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.UniqueConstraint("tenant_id", "owner_user_id", "name", name="uq_zr_knowledge_base_owner_name"),
    )
    op.create_index("ix_zr_knowledge_base_tenant_id", "zr_knowledge_base", ["tenant_id"])
    op.create_index("ix_zr_knowledge_base_owner_user_id", "zr_knowledge_base", ["owner_user_id"])
    op.create_index("ix_zr_knowledge_base_owner_status", "zr_knowledge_base", ["owner_user_id", "status"])

    op.create_table(
        "zr_knowledge_base_document",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("knowledge_base_id", sa.String(36), sa.ForeignKey("zr_knowledge_base.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_id", sa.String(36), sa.ForeignKey("zr_file.id", ondelete="RESTRICT"), nullable=False),
        sa.UniqueConstraint("knowledge_base_id", "file_id", name="uq_zr_kb_document"),
    )
    op.create_index("ix_zr_knowledge_base_document_tenant_id", "zr_knowledge_base_document", ["tenant_id"])
    op.create_index("ix_zr_kb_document_file", "zr_knowledge_base_document", ["file_id"])


def downgrade() -> None:
    op.drop_index("ix_zr_kb_document_file", table_name="zr_knowledge_base_document")
    op.drop_index("ix_zr_knowledge_base_document_tenant_id", table_name="zr_knowledge_base_document")
    op.drop_table("zr_knowledge_base_document")
    op.drop_index("ix_zr_knowledge_base_owner_status", table_name="zr_knowledge_base")
    op.drop_index("ix_zr_knowledge_base_owner_user_id", table_name="zr_knowledge_base")
    op.drop_index("ix_zr_knowledge_base_tenant_id", table_name="zr_knowledge_base")
    op.drop_table("zr_knowledge_base")
