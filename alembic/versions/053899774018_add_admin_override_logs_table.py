"""add_admin_override_logs_table

Revision ID: 053899774018
Revises: r0u1v2w3x4
Create Date: 2026-04-08 11:00:00.812210

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '053899774018'
down_revision: Union[str, Sequence[str], None] = 'r0u1v2w3x4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - Create admin_override_logs table."""
    op.create_table(
        'admin_override_logs',
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('admin_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('admin_role', sa.String(50), nullable=False),
        sa.Column('target_scope', sa.String(255), nullable=False),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('accessed_resource_type', sa.String(50), nullable=False),
        sa.Column('accessed_resource_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('override_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['admin_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_admin_override_logs_admin_id', 'admin_override_logs', ['admin_id'])
    op.create_index('ix_admin_override_logs_created_at', 'admin_override_logs', ['created_at'])


def downgrade() -> None:
    """Downgrade schema - Drop admin_override_logs table."""
    op.drop_index('ix_admin_override_logs_created_at', table_name='admin_override_logs')
    op.drop_index('ix_admin_override_logs_admin_id', table_name='admin_override_logs')
    op.drop_table('admin_override_logs')
