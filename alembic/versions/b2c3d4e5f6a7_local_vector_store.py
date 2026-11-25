"""Add persistent local vector records.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
"""

from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "zr_knowledge_vector",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("namespace", sa.String(512), nullable=False),
        sa.Column("record_id", sa.String(64), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("vector_data", sa.LargeBinary(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.UniqueConstraint("namespace", "record_id", name="uq_zr_knowledge_vector_namespace_record"),
    )


def downgrade() -> None:
    op.drop_table("zr_knowledge_vector")
