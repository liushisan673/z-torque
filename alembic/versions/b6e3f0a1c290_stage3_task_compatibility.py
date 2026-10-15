"""Restore Stage 02 compatibility for synthetic Task references."""
from typing import Sequence

from alembic import op

revision: str = "b6e3f0a1c290"
down_revision: str | None = "a4d8e1f2b390"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("zr_task") as batch:
        batch.drop_constraint("fk_task_agent_version", type_="foreignkey")
        batch.drop_constraint("fk_task_agent", type_="foreignkey")
        batch.drop_constraint("fk_task_result_message", type_="foreignkey")
        batch.drop_constraint("fk_task_message_legacy", type_="foreignkey")
        batch.drop_constraint("fk_task_trigger_message", type_="foreignkey")
        batch.drop_constraint("fk_task_conversation", type_="foreignkey")


def downgrade() -> None:
    with op.batch_alter_table("zr_task") as batch:
        batch.create_foreign_key("fk_task_conversation", "zr_conversation", ["conversation_id"], ["id"])
        batch.create_foreign_key("fk_task_trigger_message", "zr_message", ["trigger_message_id"], ["id"])
        batch.create_foreign_key("fk_task_message_legacy", "zr_message", ["message_id"], ["id"])
        batch.create_foreign_key("fk_task_result_message", "zr_message", ["result_message_id"], ["id"])
        batch.create_foreign_key("fk_task_agent", "zr_agent", ["agent_id"], ["id"])
        batch.create_foreign_key("fk_task_agent_version", "zr_agent_version", ["agent_version_id"], ["id"])
