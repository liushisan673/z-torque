"""add task delegation uniqueness constraints

Revision ID: 6e2d7a1b4c90
Revises: 4f8a1c2d6e90
"""

from typing import Sequence, Union

from alembic import op

revision: str = "6e2d7a1b4c90"
down_revision: Union[str, None] = "4f8a1c2d6e90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_task_parent_child_sequence",
        "zr_task",
        ["tenant_id", "parent_task_id", "child_sequence"],
        unique=True,
    )
    op.create_index(
        "uq_task_delegation",
        "zr_task",
        ["tenant_id", "delegation_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_task_delegation", table_name="zr_task")
    op.drop_index("uq_task_parent_child_sequence", table_name="zr_task")
