"""add the missing ix_issues_blocked_by_id index

Revision ID: t2u3v4w5x6
Revises: s1t2u3v4w5
Create Date: 2026-07-28

``Issue.blocked_by_id`` is declared ``index=True`` on the model but the index was
never created in the database — ``alembic check`` reported it as drift. It is a
foreign key with ``ON DELETE SET NULL``, and Postgres does not index the
referencing side automatically, so every delete of a user has to sequentially
scan ``issues`` to find rows pointing at them.

``CONCURRENTLY`` is deliberately not used: it cannot run inside a transaction,
and Alembic wraps each migration in one. The table is small enough here that a
brief lock is fine; on a large production table, create it out of band instead.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "t2u3v4w5x6"
down_revision: Union[str, Sequence[str], None] = "s1t2u3v4w5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_issues_blocked_by_id", "issues", ["blocked_by_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_issues_blocked_by_id", table_name="issues")
