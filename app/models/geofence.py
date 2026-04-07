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
    
    # Audit fields
    created_by_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    created_by = relationship("User")
