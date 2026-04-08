"""add_admin_messages_table

Revision ID: d0aefc5d3a75
Revises: 053899774018
Create Date: 2026-04-08 11:00:31.614880

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd0aefc5d3a75'
down_revision: Union[str, Sequence[str], None] = '053899774018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - Create admin_messages table."""
    op.create_table(
        'admin_messages',
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('sender_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('receiver_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('subject', sa.String(255), nullable=False),
        sa.Column('body', sa.Text, nullable=False),
        sa.Column('message_type', sa.String(50), server_default='general'),
        sa.Column('related_resource_type', sa.String(50), nullable=True),
        sa.Column('related_resource_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('is_read', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_urgent', sa.String(50), server_default='normal'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['sender_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['receiver_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_admin_messages_receiver_id', 'admin_messages', ['receiver_id'])
    op.create_index('ix_admin_messages_sender_id', 'admin_messages', ['sender_id'])
    op.create_index('ix_admin_messages_created_at', 'admin_messages', ['created_at'])


def downgrade() -> None:
    """Downgrade schema - Drop admin_messages table."""
    op.drop_index('ix_admin_messages_created_at', table_name='admin_messages')
    op.drop_index('ix_admin_messages_sender_id', table_name='admin_messages')
    op.drop_index('ix_admin_messages_receiver_id', table_name='admin_messages')
    op.drop_table('admin_messages')
