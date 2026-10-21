"""tool binding governance: tool tags/business flag, skill orchestration hint, binding order

Revision ID: c1d2e3f4a5b6
Revises: 2f5c8d1a4b73
"""
from alembic import op
import sqlalchemy as sa

revision = "c1d2e3f4a5b6"
down_revision = "2f5c8d1a4b73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Tool 级可变元数据：标签与业务封装标识（不参与版本快照）
    op.add_column("zr_tool", sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("zr_tool", sa.Column("is_business_tool", sa.Boolean(), nullable=False, server_default="0"))
    # SkillVersion 级调用顺序提示：随版本发布固化
    op.add_column("zr_skill_version", sa.Column("orchestration_hint", sa.Text(), nullable=True))
    # 绑定关系扩展：顺序、角色、依赖备注
    op.add_column("zr_skill_version_tool", sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("zr_skill_version_tool", sa.Column("role", sa.String(length=16), nullable=True))
    op.add_column("zr_skill_version_tool", sa.Column("note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("zr_skill_version_tool", "note")
    op.drop_column("zr_skill_version_tool", "role")
    op.drop_column("zr_skill_version_tool", "sort_order")
    op.drop_column("zr_skill_version", "orchestration_hint")
    op.drop_column("zr_tool", "is_business_tool")
    op.drop_column("zr_tool", "tags")
