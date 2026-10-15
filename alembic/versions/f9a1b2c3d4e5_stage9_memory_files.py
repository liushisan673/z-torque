"""Stage 9 memory and file resources."""
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f9a1b2c3d4e5"
down_revision: str | tuple[str, str] = ("a2b3c4d5e6f7", "e7f8a9b0c1d2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    common = [sa.Column("id", sa.String(36), primary_key=True), sa.Column("tenant_id", sa.String(36), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False)]
    op.create_table("zr_memory_short_term", *common, sa.Column("user_id", sa.String(36), sa.ForeignKey("zr_user.id"), nullable=False), sa.Column("agent_id", sa.String(36), sa.ForeignKey("zr_agent.id"), nullable=False), sa.Column("conversation_id", sa.String(36), sa.ForeignKey("zr_conversation.id", ondelete="CASCADE"), nullable=False), sa.Column("summary", sa.Text(), nullable=False), sa.Column("source_start", sa.Integer(), nullable=False), sa.Column("source_end", sa.Integer(), nullable=False), sa.Column("source_message_ids", sa.JSON(), nullable=False), sa.Column("status", sa.String(16), nullable=False))
    op.create_index("ix_zr_memory_short_term_conversation_id", "zr_memory_short_term", ["conversation_id"])
    op.create_table("zr_file", *common, sa.Column("owner_user_id", sa.String(36), sa.ForeignKey("zr_user.id"), nullable=False), sa.Column("conversation_id", sa.String(36), sa.ForeignKey("zr_conversation.id", ondelete="SET NULL")), sa.Column("original_name", sa.String(255), nullable=False), sa.Column("media_type", sa.String(128), nullable=False), sa.Column("size_bytes", sa.Integer(), nullable=False), sa.Column("sha256", sa.String(64), nullable=False), sa.Column("storage_key", sa.String(128), nullable=False, unique=True), sa.Column("status", sa.String(24), nullable=False), sa.Column("parse_error", sa.Text()), sa.Column("extracted_text", sa.Text()))
    op.create_table("zr_message_file", sa.Column("message_id", sa.String(36), sa.ForeignKey("zr_message.id", ondelete="CASCADE"), primary_key=True), sa.Column("file_id", sa.String(36), sa.ForeignKey("zr_file.id", ondelete="RESTRICT"), primary_key=True), sa.Column("tenant_id", sa.String(36), sa.ForeignKey("zr_tenant.id"), nullable=False))


def downgrade() -> None:
    op.drop_table("zr_message_file")
    op.drop_table("zr_file")
    op.drop_index("ix_zr_memory_short_term_conversation_id", table_name="zr_memory_short_term")
    op.drop_table("zr_memory_short_term")