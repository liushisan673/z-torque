"""stage 3 agent snapshots and conversations"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op
from app.db.base import GUID

revision: str = "3c7e2f1a9d04"
down_revision: Union[str, None] = "0a3c9f48b0b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table("zr_agent",
        sa.Column("id", GUID(), primary_key=True), sa.Column("tenant_id", GUID(), nullable=False),
        sa.Column("owner_user_id", GUID(), nullable=True), sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True), sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_agent_id", GUID(), nullable=True), sa.Column("source_agent_version_id", GUID(), nullable=True),
        sa.Column("current_published_version_id", GUID(), nullable=True), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["zr_user.id"]), sa.ForeignKeyConstraint(["source_agent_id"], ["zr_agent.id"]),
    )
    op.create_index("ix_zr_agent_tenant_id", "zr_agent", ["tenant_id"])
    op.create_index("ix_zr_agent_owner_user_id", "zr_agent", ["owner_user_id"])
    op.create_table("zr_agent_version",
        sa.Column("id", GUID(), primary_key=True), sa.Column("tenant_id", GUID(), nullable=False),
        sa.Column("agent_id", GUID(), nullable=False), sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False), sa.Column("config_schema_version", sa.String(32), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False), sa.Column("snapshot_hash", sa.String(128), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.ForeignKeyConstraint(["agent_id"], ["zr_agent.id"]),
        sa.UniqueConstraint("agent_id", "version_number"),
    )
    op.create_index("ix_zr_agent_version_agent_id", "zr_agent_version", ["agent_id"])
    op.create_index("ix_zr_agent_version_status", "zr_agent_version", ["status"])
    op.create_table("zr_agent_version_skill",
        sa.Column("id", GUID(), primary_key=True), sa.Column("tenant_id", GUID(), nullable=False),
        sa.Column("agent_version_id", GUID(), nullable=False), sa.Column("skill_version_id", GUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_version_id"], ["zr_agent_version.id"], ondelete="RESTRICT"), sa.UniqueConstraint("agent_version_id", "skill_version_id"),
    )
    op.create_table("zr_conversation",
        sa.Column("id", GUID(), primary_key=True), sa.Column("tenant_id", GUID(), nullable=False), sa.Column("owner_user_id", GUID(), nullable=False),
        sa.Column("agent_id", GUID(), nullable=False), sa.Column("title", sa.String(256), nullable=True), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True), sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["zr_user.id"]), sa.ForeignKeyConstraint(["agent_id"], ["zr_agent.id"]),
    )
    op.create_index("ix_zr_conversation_owner_user_id", "zr_conversation", ["owner_user_id"])
    op.create_table("zr_message",
        sa.Column("id", GUID(), primary_key=True), sa.Column("tenant_id", GUID(), nullable=False), sa.Column("conversation_id", GUID(), nullable=False),
        sa.Column("sender_type", sa.String(16), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("content_parts", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False), sa.Column("task_id", GUID(), nullable=True), sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["zr_conversation.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["task_id"], ["zr_task.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("task_id"),
    )
    op.create_index("ix_zr_message_conversation_id", "zr_message", ["conversation_id"])
    op.add_column("zr_task", sa.Column("result_message_id", GUID(), nullable=True))


def downgrade() -> None:
    op.drop_column("zr_task", "result_message_id")
    op.drop_table("zr_message")
    op.drop_table("zr_conversation")
    op.drop_table("zr_agent_version_skill")
    op.drop_table("zr_agent_version")
    op.drop_table("zr_agent")