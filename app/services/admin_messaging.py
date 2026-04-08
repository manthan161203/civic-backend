"""
Admin Messaging Service — Inter-Admin Communication
===================================================
Provides a messaging system for admins to communicate across hierarchy levels.
Used for escalation coordination, incident reporting, and cross-level communication.

ADMIN ROLES AUDIT - GAP #1: Admin-to-Admin Communication
- Enables admins to send messages up/down the hierarchy
- Messages about issues, workers, incidents, and general coordination
- Notification integration for real-time alerts
"""

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, Session

from app.core.logger import get_logger
from app.database import Base
from app.models import User

logger = get_logger(__name__)


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


def send_admin_message(
    sender: User,
    receiver_id: uuid.UUID,
    subject: str,
    body: str,
    message_type: str = "general",
    related_resource_type: Optional[str] = None,
    related_resource_id: Optional[uuid.UUID] = None,
    is_urgent: str = "normal",
    db: Optional[Session] = None,
) -> AdminMessage:
    """
    Send a message from one admin to another.
    
    Args:
        sender: Admin sending the message
        receiver_id: ID of receiving admin
        subject: Message subject
        body: Message body
        message_type: Type of message (general, issue, worker, incident, escalation)
        related_resource_type: "issue", "worker", or "incident"
        related_resource_id: ID of related resource
        is_urgent: "normal", "urgent", or "critical"
        db: Database session
    
    Returns:
        Created AdminMessage
    """
    if not db:
        raise ValueError("Database session required")
    
    message = AdminMessage(
        id=uuid.uuid4(),
        sender_id=sender.id,
        receiver_id=receiver_id,
        subject=subject,
        body=body,
        message_type=message_type,
        related_resource_type=related_resource_type,
        related_resource_id=related_resource_id,
        is_urgent=is_urgent,
    )
    
    db.add(message)
    db.commit()
    db.refresh(message)
    
    logger.info(
        f"Admin message sent: {sender.id} → {receiver_id}. "
        f"Subject: {subject}. Urgent: {is_urgent}",
        extra={"audit": True}
    )
    
    return message


def get_inbox(admin_id: uuid.UUID, unread_only: bool = False, db: Optional[Session] = None) -> List[AdminMessage]:
    """Get messages for an admin (inbox)."""
    if not db:
        return []
    
    query = db.query(AdminMessage).filter(AdminMessage.receiver_id == admin_id)
    if unread_only:
        query = query.filter(AdminMessage.is_read.is_(None))
    
    return query.order_by(AdminMessage.created_at.desc()).all()


def get_sent_messages(admin_id: uuid.UUID, db: Optional[Session] = None) -> List[AdminMessage]:
    """Get messages sent by an admin (sent items)."""
    if not db:
        return []
    
    return (
        db.query(AdminMessage)
        .filter(AdminMessage.sender_id == admin_id)
        .order_by(AdminMessage.created_at.desc())
        .all()
    )


def mark_read(message_id: uuid.UUID, db: Optional[Session] = None) -> AdminMessage:
    """Mark a message as read."""
    if not db:
        raise ValueError("Database session required")
    
    message = db.query(AdminMessage).filter(AdminMessage.id == message_id).first()
    if message:
        message.is_read = datetime.utcnow()
        db.commit()
        db.refresh(message)
    
    return message


def get_unread_count(admin_id: uuid.UUID, db: Optional[Session] = None) -> int:
    """Get count of unread messages for an admin."""
    if not db:
        return 0
    
    return (
        db.query(AdminMessage)
        .filter(
            AdminMessage.receiver_id == admin_id,
            AdminMessage.is_read.is_(None)
        )
        .count()
    )
