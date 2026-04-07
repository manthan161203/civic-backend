"""Add blocked task tracking and admin unblock capabilities.

Revision ID: q6r7s8t9u0
Revises: p5q6r7s8t9
Create Date: 2026-04-07 15:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'q6r7s8t9u0'
down_revision = 'p5q6r7s8t9'
branch_labels = None
depends_on = None


def upgrade():
    # Add new columns to issues table for blocking tracking
    op.add_column('issues', sa.Column('blocked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('issues', sa.Column('blocked_by_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('issues', sa.Column('unblocked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('issues', sa.Column('unblocked_by_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('issues', sa.Column('admin_unblock_note', sa.Text(), nullable=True))
    op.add_column('issues', sa.Column('block_resolved_by', sa.String(50), nullable=True))
    
    # Create index on is_blocked for filtering
    op.create_index('ix_issues_is_blocked', 'issues', ['is_blocked'])
    
    # Create index on blocked_at for sorting by block duration
    op.create_index('ix_issues_blocked_at', 'issues', ['blocked_at'])
    
    # Add foreign key for blocked_by_id
    op.create_foreign_key('fk_issues_blocked_by_id', 'issues', 'users', ['blocked_by_id'], ['id'])
    
    # Add foreign key for unblocked_by_id
    op.create_foreign_key('fk_issues_unblocked_by_id', 'issues', 'users', ['unblocked_by_id'], ['id'])


def downgrade():
    # Drop foreign keys
    op.drop_constraint('fk_issues_unblocked_by_id', 'issues', type_='foreignkey')
    op.drop_constraint('fk_issues_blocked_by_id', 'issues', type_='foreignkey')
    
    # Drop indexes
    op.drop_index('ix_issues_blocked_at')
    op.drop_index('ix_issues_is_blocked')
    
    # Drop columns
    op.drop_column('issues', 'block_resolved_by')
    op.drop_column('issues', 'admin_unblock_note')
    op.drop_column('issues', 'unblocked_by_id')
    op.drop_column('issues', 'unblocked_at')
    op.drop_column('issues', 'blocked_by_id')
    op.drop_column('issues', 'blocked_at')
