"""add_admin_overrides_table

Revision ID: c8d9e0f1g2h
Revises: d0aefc5d3a75
Create Date: 2026-04-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8d9e0f1g2h'
down_revision: Union[str, Sequence[str], None] = 'd0aefc5d3a75'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - Create admin_overrides table to store active/granted overrides."""
    op.create_table(
        'admin_overrides',
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('granted_by_admin_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('granted_to_admin_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('scope_level', sa.String(50), nullable=False),  # 'ward', 'taluka', 'district'
        sa.Column('target_scope_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('reason', sa.Text, nullable=False),
        sa.Column('override_until', sa.DateTime(timezone=True), nullable=True),  # NULL = permanent
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['granted_by_admin_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['granted_to_admin_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['revoked_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.Index('ix_admin_overrides_granted_to_admin_id', 'granted_to_admin_id'),
        sa.Index('ix_admin_overrides_granted_by_admin_id', 'granted_by_admin_id'),
        sa.Index('ix_admin_overrides_created_at', 'created_at'),
        sa.Index('ix_admin_overrides_revoked_at', 'revoked_at'),
        sa.Index('ix_admin_overrides_active', 'granted_to_admin_id', 'revoked_at', postgresql_where=sa.text("revoked_at IS NULL")),
    )


def downgrade() -> None:
    """Downgrade schema - Drop admin_overrides table."""
    op.drop_table('admin_overrides')
