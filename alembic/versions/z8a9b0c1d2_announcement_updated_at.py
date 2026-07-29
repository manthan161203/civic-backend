"""announcements.updated_at

Announcements could be created and deleted but never edited — there was no
``PATCH /admin/announcements/{id}``, and the admin console shipped a fully built
Edit modal wired to a toast explaining the backend did not support it. Fixing a
typo in a message already delivered to a district meant deleting it and posting
a replacement, which re-notified everyone.

Adding the endpoint makes the row mutable, and a mutable row needs to say when
it last changed: ``push_dispatched_at`` records when the text went out, and
without ``updated_at`` there is no way to tell that the text now on screen is
not the text that was delivered.

Backfilled to ``created_at`` rather than ``now()``, so existing announcements do
not all claim to have been edited at migration time.

Revision ID: z8a9b0c1d2
Revises: y7z8a9b0c1
Create Date: 2026-07-29

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "z8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = "y7z8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Added nullable so the table can be rewritten without a default, then
    # backfilled, then made NOT NULL — the usual three-step so an existing
    # deployment is not locked while every row is stamped.
    op.add_column(
        "announcements",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE announcements SET updated_at = created_at WHERE updated_at IS NULL")
    op.alter_column(
        "announcements",
        "updated_at",
        nullable=False,
        server_default=sa.text("now()"),
    )


def downgrade() -> None:
    op.drop_column("announcements", "updated_at")
