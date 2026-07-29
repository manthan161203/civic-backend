"""Record what an offline action created, so a replay can return the same id.

`POST /sync` gained a `create_issue` action for reports composed while
disconnected. It answers with the new issue's UUID so the client can then upload
the photos it could not send at the time.

Replays are the normal case for this endpoint — it exists precisely because
connections drop — and the dedup path short-circuits before any handler runs. So
without somewhere to remember the id, a replayed `create_issue` would come back
`duplicate: true` with nothing attached, and the photos held on the device would
have no issue to attach to. That is the column this adds.

Nullable: only `create_issue` sets it, and every row written before this
migration predates the feature.

Revision ID: y7z8a9b0c1
Revises: x6y7z8a9b0
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "y7z8a9b0c1"
down_revision: Union[str, Sequence[str], None] = "x6y7z8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "synced_actions",
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("synced_actions", "resource_id")
