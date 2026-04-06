"""
OTP Model — Short-lived one-time passwords for phone authentication
===================================================================
Each send-OTP request creates a new OTP record. Validation:
  - ``is_used=False`` — not yet consumed.
  - ``expires_at``    — defaults to 10 minutes after creation.

Expired and used OTPs are purged hourly by the auto-escalation background task.
Rate limiting (1 OTP per 60 s per phone) is enforced in the route layer.
"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class OTP(Base):
    __tablename__ = "otps"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone = Column(String, nullable=False, index=True)
    code = Column(String(6), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    is_used = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
