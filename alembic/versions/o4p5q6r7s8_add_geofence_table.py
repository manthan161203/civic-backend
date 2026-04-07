"""add_geofence_table - Create geofences table for location-based notifications

Revision ID: o4p5q6r7s8
Revises: n3o4p5q6r7
Create Date: 2026-04-07

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "o4p5q6r7s8"
down_revision = "n3o4p5q6r7"
branch_labels = None
depends_on = None


def upgrade():
    """
    Create geofences table for location-based notifications.
    
    Geofences define circular zones (center + radius) for:
    - Targeted notifications to users within a zone
    - Location-based alerts
    - Worker monitoring
    """
    op.create_table(
        "geofences",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("radius_km", sa.Float(), nullable=False),
        sa.Column(
            "created_by_id",
            UUID(as_uuid=True),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_geofence_created_by_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    """
    Drop geofences table and related indexes.
    """
    op.drop_table("geofences")
