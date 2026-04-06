"""add reassignment_count and soft-delete to issues

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-03-30 00:00:00.000000

Changes:
    issues.reassignment_count  — tracks how many times an issue has been rejected/reassigned
    issues.is_deleted          — soft-delete flag (hidden from all normal views when True)
    issues.deleted_at          — timestamp of soft-deletion
"""

from alembic import op
import sqlalchemy as sa

revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "issues",
        sa.Column("reassignment_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "issues",
        sa.Column("is_deleted", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "issues",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_issues_is_deleted", "issues", ["is_deleted"])


def downgrade() -> None:
    op.drop_index("ix_issues_is_deleted", table_name="issues")
    op.drop_column("issues", "deleted_at")
    op.drop_column("issues", "is_deleted")
    op.drop_column("issues", "reassignment_count")
