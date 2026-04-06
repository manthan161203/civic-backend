"""add sos fields and issue_squads table

Revision ID: h7c8d9e0f1g2
Revises: g6b7c8d9e0f1
Create Date: 2026-04-03

Adds:
- Issue.is_sos (boolean, default false)
- Issue.sos_radius_notified (boolean, default false)
- issue_squads table for multi-worker squad assignments
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "h7c8d9e0f1g2"
down_revision = "f7bdeda7fdb3"
branch_labels = None
depends_on = None


def upgrade():
    # SOS fields on issues table
    op.add_column("issues", sa.Column("is_sos", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("issues", sa.Column("sos_radius_notified", sa.Boolean(), server_default="false", nullable=False))

    # Squad status enum
    squad_status = postgresql.ENUM("active", "completed", "disbanded", name="squad_status", create_type=False)
    squad_status.create(op.get_bind(), checkfirst=True)

    # Issue squads table
    op.create_table(
        "issue_squads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("issue_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("lead_worker_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=False),
        sa.Column("assistant_ids", postgresql.JSON(), server_default="[]", nullable=False),
        sa.Column("status", squad_status, server_default="active", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_issue_squads_issue_id", "issue_squads", ["issue_id"])
    op.create_index("ix_issue_squads_lead_worker_id", "issue_squads", ["lead_worker_id"])


def downgrade():
    op.drop_table("issue_squads")
    op.execute("DROP TYPE IF EXISTS squad_status")
    op.drop_column("issues", "sos_radius_notified")
    op.drop_column("issues", "is_sos")
