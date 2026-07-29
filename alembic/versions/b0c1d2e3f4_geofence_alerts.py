"""geofence_alerts

``POST /admin/notifications/geofence`` pushed a title and body to every user
inside a circle, logged a recipient count, and persisted nothing. So there was
no answer to "how often is this zone alerted", "who sent Tuesday's warning", or
"did it reach anyone" — and no way to reconstruct them, because the data was
never recorded.

One row per dispatch. Nothing is back-filled: every broadcast before this table
existed left no trace, and inventing rows for them would be worse than the gap.

``geofence_id`` is nullable by design — the endpoint broadcasts to an ad-hoc
circle supplied in the request, which need not correspond to a saved zone. The
circle itself is stored alongside so the record still describes what happened
after the zone is later moved, resized or deleted.

Revision ID: b0c1d2e3f4
Revises: a9b0c1d2e3
Create Date: 2026-07-29

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b0c1d2e3f4"
down_revision: Union[str, Sequence[str], None] = "a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "geofence_alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # SET NULL, not CASCADE: deleting a zone must not erase the record that
        # it was used to alert people.
        sa.Column(
            "geofence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("geofences.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("radius_km", sa.Float(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "sent_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("recipients_notified", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recipients_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "ward_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("wards.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "taluka_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("talukas.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "district_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("districts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    for column in ("geofence_id", "sent_by_id", "ward_id", "taluka_id", "district_id", "created_at"):
        op.create_index(f"ix_geofence_alerts_{column}", "geofence_alerts", [column])


def downgrade() -> None:
    op.drop_table("geofence_alerts")
