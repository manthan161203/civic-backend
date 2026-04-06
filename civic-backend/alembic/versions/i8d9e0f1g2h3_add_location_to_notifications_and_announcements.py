"""add location to notifications and announcements

Revision ID: i8d9e0f1g2h3
Revises: h7c8d9e0f1g2
Create Date: 2026-04-03
"""

from alembic import op
import sqlalchemy as sa

revision = 'i8d9e0f1g2h3'
down_revision = 'h7c8d9e0f1g2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('notifications', sa.Column('location_lat', sa.Float(), nullable=True))
    op.add_column('notifications', sa.Column('location_lng', sa.Float(), nullable=True))
    op.add_column('announcements', sa.Column('location_lat', sa.Float(), nullable=True))
    op.add_column('announcements', sa.Column('location_lng', sa.Float(), nullable=True))


def downgrade():
    op.drop_column('announcements', 'location_lng')
    op.drop_column('announcements', 'location_lat')
    op.drop_column('notifications', 'location_lng')
    op.drop_column('notifications', 'location_lat')
