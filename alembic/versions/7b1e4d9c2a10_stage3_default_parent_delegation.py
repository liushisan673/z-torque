"""Add Stage 03 default parent and delegation snapshot fields."""
from typing import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7b1e4d9c2a10"
down_revision: str | None = "6e2d7a1b4c90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("zr_agent", sa.Column("is_default_parent", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index(
        "uq_zr_agent_default_parent",
        "zr_agent",
        ["is_default_parent"],
        unique=True,
        sqlite_where=sa.text("is_default_parent = 1"),
    )
    op.add_column("zr_agent_version", sa.Column("delegation_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("zr_agent_version", sa.Column("allowed_child_agent_versions", sa.JSON(), nullable=True))
    op.add_column("zr_agent_version", sa.Column("child_necessity_policy", sa.JSON(), nullable=True))
    op.add_column("zr_agent_version", sa.Column("delegation_capability_version", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("zr_agent_version", "delegation_capability_version")
    op.drop_column("zr_agent_version", "child_necessity_policy")
    op.drop_column("zr_agent_version", "allowed_child_agent_versions")
    op.drop_column("zr_agent_version", "delegation_enabled")
    op.drop_index("uq_zr_agent_default_parent", table_name="zr_agent")
    op.drop_column("zr_agent", "is_default_parent")
