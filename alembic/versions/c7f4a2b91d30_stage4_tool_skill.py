"""Stage 04 Tool, Skill, confirmation, and execution log schema."""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c7f4a2b91d30"
down_revision: str | None = "b6e3f0a1c290"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = [
        ("zr_tool", [
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant_id", sa.String(36), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("owner_user_id", sa.String(36)), sa.Column("name", sa.String(128), nullable=False), sa.Column("description", sa.Text()),
            sa.Column("tool_type", sa.String(32), nullable=False), sa.Column("source_type", sa.String(16), nullable=False), sa.Column("status", sa.String(16), nullable=False), sa.Column("official", sa.Boolean(), nullable=False), sa.Column("current_published_version_id", sa.String(36)),
        ]),
        ("zr_skill", [
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant_id", sa.String(36), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), sa.Column("owner_user_id", sa.String(36)), sa.Column("name", sa.String(128), nullable=False), sa.Column("description", sa.Text()), sa.Column("skill_type", sa.String(32), nullable=False), sa.Column("source_type", sa.String(16), nullable=False), sa.Column("status", sa.String(16), nullable=False), sa.Column("official", sa.Boolean(), nullable=False), sa.Column("current_published_version_id", sa.String(36)),
        ]),
    ]
    for name, columns in tables:
        op.create_table(name, *columns)
    op.create_table("zr_tool_version", sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant_id", sa.String(36), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), sa.Column("tool_id", sa.String(36), sa.ForeignKey("zr_tool.id", ondelete="RESTRICT"), nullable=False), sa.Column("version_number", sa.Integer(), nullable=False), sa.Column("status", sa.String(16), nullable=False), sa.Column("config_schema_version", sa.String(32), nullable=False), sa.Column("config_snapshot", sa.JSON(), nullable=False), sa.Column("input_schema", sa.JSON(), nullable=False), sa.Column("output_schema", sa.JSON()), sa.Column("required_permissions", sa.JSON(), nullable=False), sa.Column("snapshot_hash", sa.String(128)), sa.Column("published_at", sa.DateTime()), sa.UniqueConstraint("tool_id", "version_number"))
    op.create_table("zr_skill_version", sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant_id", sa.String(36), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), sa.Column("skill_id", sa.String(36), sa.ForeignKey("zr_skill.id", ondelete="RESTRICT"), nullable=False), sa.Column("version_number", sa.Integer(), nullable=False), sa.Column("status", sa.String(16), nullable=False), sa.Column("config_schema_version", sa.String(32), nullable=False), sa.Column("config_snapshot", sa.JSON(), nullable=False), sa.Column("instructions", sa.Text()), sa.Column("script_source", sa.Text()), sa.Column("required_permissions", sa.JSON(), nullable=False), sa.Column("snapshot_hash", sa.String(128)), sa.Column("published_at", sa.DateTime()), sa.UniqueConstraint("skill_id", "version_number"))
    op.create_table("zr_skill_version_tool", sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant_id", sa.String(36), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), sa.Column("skill_version_id", sa.String(36), sa.ForeignKey("zr_skill_version.id", ondelete="RESTRICT"), nullable=False), sa.Column("tool_version_id", sa.String(36), sa.ForeignKey("zr_tool_version.id", ondelete="RESTRICT"), nullable=False), sa.UniqueConstraint("skill_version_id", "tool_version_id"))
    op.create_table("zr_skill_execution_log", sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant_id", sa.String(36), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), sa.Column("task_id", sa.String(36), sa.ForeignKey("zr_task.id", ondelete="SET NULL")), sa.Column("skill_version_id", sa.String(36), sa.ForeignKey("zr_skill_version.id", ondelete="RESTRICT"), nullable=False), sa.Column("tool_version_id", sa.String(36), sa.ForeignKey("zr_tool_version.id", ondelete="RESTRICT")), sa.Column("actor_user_id", sa.String(36)), sa.Column("status", sa.String(32), nullable=False), sa.Column("result", sa.JSON()), sa.Column("error_code", sa.String(64)), sa.Column("duration_ms", sa.Integer()))
    op.create_table("zr_confirmation", sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant_id", sa.String(36), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), sa.Column("task_id", sa.String(36), sa.ForeignKey("zr_task.id", ondelete="SET NULL")), sa.Column("skill_version_id", sa.String(36), sa.ForeignKey("zr_skill_version.id", ondelete="SET NULL")), sa.Column("tool_version_id", sa.String(36), sa.ForeignKey("zr_tool_version.id", ondelete="SET NULL")), sa.Column("requested_by_user_id", sa.String(36), nullable=False), sa.Column("confirmed_by_user_id", sa.String(36)), sa.Column("status", sa.String(16), nullable=False), sa.Column("reason", sa.String(512)), sa.Column("request_snapshot", sa.JSON(), nullable=False), sa.Column("resolved_at", sa.DateTime()))


def downgrade() -> None:
    for name in ("zr_confirmation", "zr_skill_execution_log", "zr_skill_version_tool", "zr_skill_version", "zr_tool_version", "zr_skill", "zr_tool"):
        op.drop_table(name)
