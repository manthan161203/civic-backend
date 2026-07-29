"""make geofences.created_by_id nullable for SET NULL

Revision ID: s1t2u3v4w5
Revises: merge_integrity_heads
Create Date: 2026-07-28 06:36:43.760938

``r0u1v2w3x4`` set ``fk_geofence_created_by_id`` to ``ON DELETE SET NULL`` under
the heading "Geofences preserved when creator deleted", but left the column
``NOT NULL``. Those two contradict: deleting the creating admin makes Postgres
try ``SET created_by_id = NULL`` and fail the not-null constraint.

    ERROR: null value in column "created_by_id" of relation "geofences"
           violates not-null constraint
    CONTEXT: SQL statement "UPDATE ONLY geofences SET created_by_id = NULL ..."

So the fix never worked — it replaced a foreign-key block on deleting an admin
with a not-null block. This drops the NOT NULL so SET NULL can do its job and
the geofence survives its creator, which was the stated intent.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 's1t2u3v4w5'
down_revision: Union[str, Sequence[str], None] = 'merge_integrity_heads'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Allow geofences.created_by_id to be NULL."""
    op.alter_column(
        "geofences",
        "created_by_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    """Restore NOT NULL, reassigning rows whose creator has been deleted.

    By this point some rows legitimately hold NULL and would violate the
    restored constraint. There is no correct owner to put back, so they are
    pointed at the oldest remaining super-admin.
    """
    op.execute(
        """
        UPDATE geofences
        SET created_by_id = (
            SELECT id FROM users
            WHERE role = 'admin'
            ORDER BY created_at
            LIMIT 1
        )
        WHERE created_by_id IS NULL
        """
    )
    op.alter_column(
        "geofences",
        "created_by_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
