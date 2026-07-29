"""
AdminMessage — inter-admin communication
========================================
Message passed between admins across hierarchy levels, used for escalation
coordination, incident reporting, and general cross-level communication.

The service layer lives in ``app/services/admin_messaging.py``; the model lives
here so that it is registered on ``Base.metadata`` whenever ``app.models`` is
imported. Keeping it in the service module hid the ``admin_messages`` table from
Alembic autogenerate and broke any importer of ``User`` that did not also import
the service (``User.messages_sent`` resolves the ``"AdminMessage"`` string).
"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class AdminMessage(Base):
    """Message between admins for coordination and escalation."""

    __tablename__ = "admin_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Sender and receiver
    sender_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    receiver_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)

    # Message content
    subject = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    message_type = Column(String(50), default="general")  # "general", "issue", "worker", "incident", "escalation"

    # Related resource (optional - can reference an issue/worker)
    related_resource_type = Column(String(50), nullable=True)  # "issue", "worker", "incident"
    related_resource_id = Column(UUID(as_uuid=True), nullable=True)

    # Status
    is_read = Column(DateTime(timezone=True), nullable=True)  # When read (null = unread)
    is_urgent = Column(String(50), default="normal")  # "normal", "urgent", "critical"

    # Audit
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    sender = relationship("User", foreign_keys=[sender_id], back_populates="messages_sent")
    receiver = relationship("User", foreign_keys=[receiver_id], back_populates="messages_received")

    def __repr__(self):
        return f"<AdminMessage {self.id} from {self.sender_id} to {self.receiver_id}>"
