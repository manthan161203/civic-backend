"""Add password and worker invitation fields to users table.

Revision ID: r7s8t9u0v1
Revises: q6r7s8t9u0
Create Date: 2026-04-07 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'r7s8t9u0v1'
down_revision = 'q6r7s8t9u0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add password_hash column
    op.add_column('users', sa.Column('password_hash', sa.String(), nullable=True))

    # Add must_change_password column with default False
    op.add_column('users', sa.Column('must_change_password', sa.Boolean(), nullable=False, server_default='false'))

    # Add invitation_sent_at column with index
    op.add_column('users', sa.Column('invitation_sent_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_users_invitation_sent_at', 'users', ['invitation_sent_at'])


def downgrade() -> None:
    # Drop index first
    op.drop_index('ix_users_invitation_sent_at', table_name='users')

    # Drop columns
    op.drop_column('users', 'invitation_sent_at')
    op.drop_column('users', 'must_change_password')
    op.drop_column('users', 'password_hash')
