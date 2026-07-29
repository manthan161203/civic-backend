"""geofences: ward_id / taluka_id / district_id

Geofences carried no geography, so every admin tier saw and could mutate every
zone in the state. ``list_geofences`` documented the gap honestly enough
("Current Geofence model does not track ward_id/taluka_id/district_id") but then
claimed "access control is enforced at creation/update/delete time" — and it was
not: ``create_geofence``, ``update_geofence`` and ``delete_geofence`` all used a
bare ``require_any_admin``. A single-ward admin could DELETE a zone belonging to
another district.

These three columns let ``apply_admin_scope`` do the work it already does for
issues. All three levels are stored denormalised, because that helper filters
directly on whichever column matches the caller's tier — a ward-level zone must
also carry its taluka and district to be visible to that ward's taluka_admin.

**Existing rows are deliberately not back-filled.**

The obvious backfill is to derive a jurisdiction from each zone's centre. There
is nothing to derive it *from*: wards carry ``centroid_lat``/``centroid_lon``
and no boundary geometry, so the best available guess is "nearest ward centroid"
— which, on a circular zone that may span several wards, would hand a ward_admin
delete rights over a zone that is not theirs. Inventing authority from a guess
is precisely the failure this migration exists to close.

So legacy rows stay all-NULL, which the model defines as state-level: visible
and mutable only by a super-admin, who can assign a jurisdiction explicitly.
That is the fail-closed direction.

Revision ID: a9b0c1d2e3
Revises: z8a9b0c1d2
Create Date: 2026-07-29

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9b0c1d2e3"
down_revision: Union[str, Sequence[str], None] = "z8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for column, target in (
        ("ward_id", "wards"),
        ("taluka_id", "talukas"),
        ("district_id", "districts"),
    ):
        op.add_column(
            "geofences",
            sa.Column(column, sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_index(f"ix_geofences_{column}", "geofences", [column])
        # SET NULL, not CASCADE: dissolving a ward must not silently delete the
        # zones inside it. The row falls back to state-level and a super-admin
        # re-assigns it, which is visible; a vanished zone is not.
        op.create_foreign_key(
            f"fk_geofences_{column}",
            "geofences",
            target,
            [column],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    for column in ("ward_id", "taluka_id", "district_id"):
        op.drop_constraint(f"fk_geofences_{column}", "geofences", type_="foreignkey")
        op.drop_index(f"ix_geofences_{column}", table_name="geofences")
        op.drop_column("geofences", column)
