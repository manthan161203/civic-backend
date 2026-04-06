"""add water to issue_type enum

Revision ID: j9e0f1g2h3
Revises: i8d9e0f1g2h3
Create Date: 2026-04-04

Adds 'water' as a valid issue type for civic issues
(e.g., water pollution, leaks, water infrastructure problems).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "j9e0f1g2h3"
down_revision = "i8d9e0f1g2h3"
branch_labels = None
depends_on = None


def upgrade():
    # Add 'water' to the existing issue_type enum
    # Skip if already exists
    op.execute("ALTER TYPE issue_type ADD VALUE IF NOT EXISTS 'water'")


def downgrade():
    # Remove 'water' from the enum by creating a new enum without it
    # and casting the column
    op.execute("""
        ALTER TABLE issues
        ALTER COLUMN issue_type TYPE VARCHAR;
        DROP TYPE issue_type;
    """)
    op.execute("""
        CREATE TYPE issue_type AS ENUM (
            'garbage', 'pothole', 'streetlight', 'drain', 'other'
        )
    """)
    op.execute("""
        ALTER TABLE issues
        ALTER COLUMN issue_type TYPE issue_type USING issue_type::issue_type
    """)
