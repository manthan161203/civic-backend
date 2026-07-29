"""
Geofence Model — Worker location zones
=======================================
Admins define circular geographic zones (geofences) for worker monitoring.
Each geofence has a center (latitude/longitude) and radius in kilometers.
Used for:
  - Location validation (is a worker within zone bounds?)
  - Notifications triggered by geofence violations
  - Worker zone assignments
"""

import uuid

from sqlalchemy import Column, DateTime, Float, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class Geofence(Base):
    __tablename__ = "geofences"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    
    # Center coordinates
    latitude = Column(Float, nullable=False)  # Range: -90 to 90
    longitude = Column(Float, nullable=False)  # Range: -180 to 180
    
    # Radius in kilometers
    radius_km = Column(Float, nullable=False)  # Must be > 0

    # ── Jurisdiction ──────────────────────────────────────────────────────────
    #
    # Geofences used to carry no geography at all, so every admin tier saw and
    # could mutate every zone in the state — a ward_admin could DELETE a zone
    # belonging to another district. The list endpoint's docstring claimed
    # "access control is enforced at creation/update/delete time"; it was not,
    # all three used a bare require_any_admin.
    #
    # All three levels are stored, denormalised, rather than just the most
    # specific one. `apply_admin_scope` filters directly on whichever column
    # matches the admin's tier, so a ward-level zone must also carry its taluka
    # and district for that ward's taluka_admin to see it.
    #
    # All NULL means state-level: visible and mutable only by a super-admin.
    # That is also what every pre-existing row keeps — see migration a9b0c1d2e3
    # for why they are not back-filled by guessing.
    ward_id = Column(
        UUID(as_uuid=True), ForeignKey("wards.id", ondelete="SET NULL"), nullable=True, index=True
    )
    taluka_id = Column(
        UUID(as_uuid=True), ForeignKey("talukas.id", ondelete="SET NULL"), nullable=True, index=True
    )
    district_id = Column(
        UUID(as_uuid=True), ForeignKey("districts.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Audit fields
    # Nullable so the FK's ON DELETE SET NULL can actually fire — see migration
    # s1t2u3v4w5. While this was NOT NULL, deleting the creating admin failed.
    created_by_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    created_by = relationship("User")
