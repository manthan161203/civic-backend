"""add district and taluka centroid coordinates

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Branch labels: None
Depends on: None

Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa

revision = "f5a6b7c8d9e0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("districts", sa.Column("centroid_lat", sa.Float(), nullable=True))
    op.add_column("districts", sa.Column("centroid_lon", sa.Float(), nullable=True))
    op.add_column("talukas", sa.Column("centroid_lat", sa.Float(), nullable=True))
    op.add_column("talukas", sa.Column("centroid_lon", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("talukas", "centroid_lon")
    op.drop_column("talukas", "centroid_lat")
    op.drop_column("districts", "centroid_lon")
    op.drop_column("districts", "centroid_lat")
