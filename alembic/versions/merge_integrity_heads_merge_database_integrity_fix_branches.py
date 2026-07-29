"""merge database integrity fix branches

Revision ID: merge_integrity_heads
Revises: r8s9t0u1v2, r9t0u1v2w3, c8d9e0f1g2h
Create Date: 2026-07-28 04:52:08.064379

Three revisions were authored against the same parent (``r7s8t9u0v1``), each a
near-identical reimplementation of the same cascade-delete fixes:

    r8s9t0u1v2  fix_critical_database_integrity
    r9t0u1v2w3  fix_database_integrity_robust
    r0u1v2w3x4  fix_database_integrity_v3  -> ... -> c8d9e0f1g2h

That left three heads, so ``alembic upgrade head`` failed outright. This is an
empty merge revision that rejoins them; the DDL is unchanged.

DOWNGRADE IS NOT SUPPORTED PAST THIS POINT. Reversing across a three-way merge
of duplicated DDL will not reconstruct a coherent schema.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'merge_integrity_heads'
down_revision: Union[str, Sequence[str], None] = ('r8s9t0u1v2', 'r9t0u1v2w3', 'c8d9e0f1g2h')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
