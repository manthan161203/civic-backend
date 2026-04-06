"""add parent_id to issue_comments for threaded replies

Revision ID: k0f1g2h3i4
Revises: j9e0f1g2h3
Create Date: 2026-04-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "k0f1g2h3i4"
down_revision = "j9e0f1g2h3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "issue_comments",
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issue_comments.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
    )


def downgrade():
    op.drop_column("issue_comments", "parent_id")
