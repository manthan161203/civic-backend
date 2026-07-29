"""
GeofenceAlert — a record that a zone broadcast happened
=======================================================

``POST /admin/notifications/geofence`` fans a push out to everyone inside a
circle and returned a recipient count that existed only in that HTTP response.
Nothing was persisted, so there was no way to answer "how often is this zone
being alerted?", "who sent the one that went out on Tuesday?", or "did that
flood warning actually reach anyone?" — and no way to build them later, because
the data was never written down.

One row per dispatch. ``geofence_id`` is nullable because the endpoint
broadcasts to an **ad-hoc circle**, not necessarily to a saved ``Geofence``: the
centre, radius, title and body are all request parameters. Storing the circle
alongside the optional zone id means a broadcast is still auditable when it did
not correspond to a saved zone at all.

The jurisdiction columns mirror ``Geofence`` for the same reason they exist
there — ``apply_admin_scope`` filters on whichever column matches the caller's
tier, so an alert has to carry the full chain to be visible to every level above
its sender. They record the *sender's* jurisdiction, which is what an audit of
"who did this" needs.

There is no backfill. Every broadcast before this table existed left no trace.
"""

import uuid

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class GeofenceAlert(Base):
    """One geofence broadcast."""

    __tablename__ = "geofence_alerts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # NULL when the broadcast targeted an ad-hoc circle rather than a saved
    # zone. SET NULL rather than CASCADE: deleting a zone must not erase the
    # record that it was used to alert people.
    geofence_id = Column(
        UUID(as_uuid=True),
        ForeignKey("geofences.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # The circle as actually broadcast. Kept even when geofence_id is set, so
    # the record still describes what happened after the zone is moved or
    # resized — an alert is a historical fact, not a view of current config.
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    radius_km = Column(Float, nullable=False)

    title = Column(String(200), nullable=False)
    body = Column(Text, nullable=False)

    sent_by_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    recipients_notified = Column(Integer, nullable=False, default=0)
    # Non-zero means some pushes failed. Previously this number was logged and
    # discarded, so a broadcast that reached nobody looked identical to one that
    # reached everybody.
    recipients_failed = Column(Integer, nullable=False, default=0)

    # Sender's jurisdiction at the time of sending — see the module docstring.
    ward_id = Column(
        UUID(as_uuid=True), ForeignKey("wards.id", ondelete="SET NULL"), nullable=True, index=True
    )
    taluka_id = Column(
        UUID(as_uuid=True), ForeignKey("talukas.id", ondelete="SET NULL"), nullable=True, index=True
    )
    district_id = Column(
        UUID(as_uuid=True), ForeignKey("districts.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    geofence = relationship("Geofence")
    sent_by = relationship("User")

    def __repr__(self):
        return f"<GeofenceAlert {self.id} zone={self.geofence_id} sent={self.recipients_notified}>"
