"""
Announcement Model — Admin broadcast messages
=============================================
Admins can broadcast messages to citizens at various scopes:
  - ward   → only citizens in a specific ward
  - taluka → all citizens in a taluka
  - district → all citizens in a district
  - state  → everyone

Citizens receive push notifications when a matching announcement is created.
"""

import uuid

from sqlalchemy import Column, DateTime, Enum, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class Announcement(Base):
    __tablename__ = "announcements"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(200), nullable=False)
    body = Column(Text, nullable=False)
    author_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)

    # Scope determines who sees this announcement
    scope = Column(
        Enum("ward", "taluka", "district", "state", name="announcement_scope"),
        nullable=False,
        default="ward",
    )

    # Target location (whichever matches the scope — others are NULL)
    ward_id = Column(UUID(as_uuid=True), ForeignKey("wards.id", ondelete="SET NULL"), nullable=True, index=True)
    taluka_id = Column(UUID(as_uuid=True), ForeignKey("talukas.id", ondelete="SET NULL"), nullable=True, index=True)
    district_id = Column(UUID(as_uuid=True), ForeignKey("districts.id", ondelete="SET NULL"), nullable=True, index=True)

    expires_at = Column(DateTime(timezone=True), nullable=True)
    location_lat = Column(Float, nullable=True)
    location_lng = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    author = relationship("User")
