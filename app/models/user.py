"""
User Model — Core user account for all roles
=============================================
A single users table covers every actor in the system:
  - citizen   — reports issues, rates resolutions, subscribes to wards
  - worker    — receives and resolves assigned tasks
  - ward_admin / taluka_admin / district_admin — scoped admin hierarchy
  - admin     — super-admin with full access across all geographies

Auth methods supported per user:
  - Phone OTP (phone column)
  - Google OAuth (google_id column)
  - Aadhaar KYC (aadhar_hash — SHA-256, never stored plaintext)
"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, Float, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Phone is nullable: Google/Aadhar users may not have a phone linked yet
    phone = Column(String, unique=True, nullable=True, index=True)
    email = Column(String, unique=True, nullable=True, index=True)
    name = Column(String, nullable=True)

    # Role hierarchy (ascending privilege):
    #   citizen < worker < ward_admin < taluka_admin < district_admin < admin (super)
    role = Column(
        Enum(
            "citizen", "worker",
            "ward_admin", "taluka_admin", "district_admin",
            "admin",                  # super-admin (state-level)
            name="user_role",
        ),
        nullable=False,
        default="citizen",
    )

    # Legacy string ward (kept for backward compat + display)
    ward = Column(String, nullable=True)

    # Structured location FK (used for hierarchy scoping)
    ward_id = Column(UUID(as_uuid=True), ForeignKey("wards.id", ondelete="SET NULL"), nullable=True, index=True)
    taluka_id = Column(UUID(as_uuid=True), ForeignKey("talukas.id", ondelete="SET NULL"), nullable=True, index=True)
    district_id = Column(UUID(as_uuid=True), ForeignKey("districts.id", ondelete="SET NULL"), nullable=True, index=True)

    # Department (workers and routing)
    # water, roads, electricity, sanitation, parks, other
    department = Column(String(50), nullable=True)

    language = Column(Enum("en", "hi", "gu", name="user_language"), nullable=False, default="en")
    is_active = Column(Boolean, default=True, nullable=False)
    is_online = Column(Boolean, default=False, nullable=False)          # app open / connected
    is_available = Column(Boolean, default=True, nullable=False)        # accepting new assignments

    fcm_token = Column(String, nullable=True)
    
    # Profile photo URL (stored by upload service)
    profile_photo_url = Column(String, nullable=True)

    # Social / national-ID auth
    google_id = Column(String, unique=True, nullable=True, index=True)
    aadhar_hash = Column(String(64), unique=True, nullable=True, index=True)  # SHA-256 of Aadhar number
    aadhar_verified = Column(Boolean, default=False, nullable=False)

    # Password-based auth (nullable for OTP/OAuth-only users)
    password_hash = Column(String, nullable=True)

    # Worker invitation tracking (pending workers who haven't changed password yet)
    must_change_password = Column(Boolean, default=False, nullable=False)
    invitation_sent_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # Worker live location (updated by worker app)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    location_updated_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    reported_issues = relationship("Issue", foreign_keys="Issue.reporter_id", back_populates="reporter")
    assigned_issues = relationship("Issue", foreign_keys="Issue.assigned_worker_id", back_populates="assigned_worker")
    notifications = relationship("Notification", back_populates="user")
    messages_sent = relationship("AdminMessage", foreign_keys="AdminMessage.sender_id", back_populates="sender")
    messages_received = relationship("AdminMessage", foreign_keys="AdminMessage.receiver_id", back_populates="receiver")
