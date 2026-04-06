"""add ward centroid coordinates

Revision ID: e4f5a6b7c8d9
Revises: b1c2d3e4f5a6
Branch labels: None
Depends on: None

Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa

revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wards", sa.Column("centroid_lat", sa.Float(), nullable=True))
    op.add_column("wards", sa.Column("centroid_lon", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("wards", "centroid_lon")
    op.drop_column("wards", "centroid_lat")
