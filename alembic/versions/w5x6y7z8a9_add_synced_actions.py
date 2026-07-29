"""add synced_actions for offline-sync replay protection

Revision ID: w5x6y7z8a9
Revises: v4w5x6y7z8
Create Date: 2026-07-28 14:55:00.000000

``POST /sync`` takes a batch of actions a worker queued while offline. Each one
carries a ``client_id`` whose schema documentation reads "Client-generated unique
ID to prevent duplicate processing" — and nothing ever compared it to anything.

That matters more than it sounds. The endpoint exists precisely because the
connection is unreliable, so the client losing a response and retrying the batch
is the expected case, not an edge case. Every retry reapplied every action:
duplicate resolution notes appended to issues, location history overwritten,
task states re-transitioned.

This table is what the ``client_id`` is now checked against. The unique
constraint on ``(user_id, client_id)`` is what makes the check safe under
concurrent retries rather than a SELECT the second request can race past.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'w5x6y7z8a9'
down_revision: Union[str, Sequence[str], None] = 'v4w5x6y7z8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "synced_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", "client_id", name="uq_synced_action"),
    )
    op.create_index("ix_synced_actions_user_id", "synced_actions", ["user_id"])
    op.create_index("ix_synced_actions_created_at", "synced_actions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_synced_actions_created_at", table_name="synced_actions")
    op.drop_index("ix_synced_actions_user_id", table_name="synced_actions")
    op.drop_table("synced_actions")
