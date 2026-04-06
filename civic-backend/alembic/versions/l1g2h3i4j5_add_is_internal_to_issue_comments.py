"""add is_internal to issue_comments for worker task notes

Revision ID: l1g2h3i4j5
Revises: k0f1g2h3i4
Create Date: 2026-04-05
"""

from alembic import op
import sqlalchemy as sa

revision = "l1g2h3i4j5"
down_revision = "k0f1g2h3i4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "issue_comments",
        sa.Column("is_internal", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade():
    op.drop_column("issue_comments", "is_internal")
