"""add users.tokens_valid_from for access-token invalidation

Revision ID: u3v4w5x6y7
Revises: t2u3v4w5x6
Create Date: 2026-07-28 13:10:00.000000

Refresh tokens are revocable — ``refresh_tokens.is_revoked`` exists for exactly
that. Access tokens are not: they are stateless bearer JWTs, and nothing on the
request path consulted that table. So "change my password because my account is
compromised" left every stolen access token working until it expired, and
``POST /auth/logout`` only ended the ability to *refresh*, not the ability to
call the API.

Adding a denylist would need a store and a lookup on every request. This column
gets the same result for free: any access token whose ``iat`` predates
``tokens_valid_from`` is rejected in ``get_current_user``. Bumping it to now()
retires every token issued so far, in one write, with no extra infrastructure.

Set by: password change, password reset, admin deactivation of a user or worker.
NULL (the default, and the value for every existing row) means "no cutoff" —
existing sessions are unaffected by this migration itself.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'u3v4w5x6y7'
down_revision: Union[str, Sequence[str], None] = 't2u3v4w5x6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the nullable cutoff column."""
    op.add_column(
        "users",
        sa.Column("tokens_valid_from", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop the column.

    Reversible without data loss in any meaningful sense: dropping it simply
    stops enforcing the cutoff, which is the pre-migration behaviour.
    """
    op.drop_column("users", "tokens_valid_from")
