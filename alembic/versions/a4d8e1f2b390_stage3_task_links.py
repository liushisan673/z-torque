"""Add Stage 03 task relationship foreign keys."""
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4d8e1f2b390"
down_revision: str | None = "9c2e7a4b1d60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("zr_task") as batch:
        batch.create_foreign_key("fk_task_conversation", "zr_conversation", ["conversation_id"], ["id"])
        batch.create_foreign_key("fk_task_trigger_message", "zr_message", ["trigger_message_id"], ["id"])
        batch.create_foreign_key("fk_task_message_legacy", "zr_message", ["message_id"], ["id"])
        batch.create_foreign_key("fk_task_result_message", "zr_message", ["result_message_id"], ["id"])
        batch.create_foreign_key("fk_task_agent", "zr_agent", ["agent_id"], ["id"])
        batch.create_foreign_key("fk_task_agent_version", "zr_agent_version", ["agent_version_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("zr_task") as batch:
        batch.drop_constraint("fk_task_agent_version", type_="foreignkey")
        batch.drop_constraint("fk_task_agent", type_="foreignkey")
        batch.drop_constraint("fk_task_result_message", type_="foreignkey")
        batch.drop_constraint("fk_task_message_legacy", type_="foreignkey")
        batch.drop_constraint("fk_task_trigger_message", type_="foreignkey")
        batch.drop_constraint("fk_task_conversation", type_="foreignkey")