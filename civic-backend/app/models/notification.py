"""
Notification Model — In-app notifications for all users
========================================================
Every significant event (task assignment, status update, resolution, escalation)
creates a Notification record. FCM push is also sent but is best-effort;
the DB record is the source of truth for the in-app inbox.

Types:
  - status_update : issue status changed (in_progress, etc.)
  - assignment    : worker assigned a new task
  - resolution    : citizen's issue resolved
  - system        : escalation, blocked task, poor AI quality, announcements
"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    issue_id = Column(UUID(as_uuid=True), ForeignKey("issues.id"), nullable=True)

    title = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    type = Column(
        Enum("status_update", "assignment", "resolution", "system", name="notification_type"),
        nullable=False,
    )
    is_read = Column(Boolean, default=False, nullable=False)
    action_type = Column(String(50), nullable=True)  # e.g. "open_issue", "rate_issue", "view_task"
    location_lat = Column(Float, nullable=True)
    location_lng = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", back_populates="notifications")
    issue = relationship("Issue", back_populates="notifications")
