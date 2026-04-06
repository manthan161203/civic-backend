"""add profile photo URL to users table

Revision ID: g6b7c8d9e0f1
Revises: f5a6b7c8d9e0
Branch labels: None
Depends on: None

Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa


revision = "g6b7c8d9e0f1"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("profile_photo_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "profile_photo_url")
