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
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core.time import now_utc
from app.core.logger import get_logger
from app.models import AdminMessage, User

logger = get_logger(__name__)

# ``AdminMessage`` moved to app/models/admin_message.py so it is registered on
# Base.metadata with every other model. Re-exported here for existing importers.


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


def get_inbox(
    admin_id: uuid.UUID,
    unread_only: bool = False,
    db: Optional[Session] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[AdminMessage]:
    """Get messages for an admin (inbox)."""
    if not db:
        return []
    
    # joinedload: the route renders m.sender.name for every row, which without
    # this issues one SELECT per message.
    query = (
        db.query(AdminMessage)
        .options(joinedload(AdminMessage.sender), joinedload(AdminMessage.receiver))
        .filter(AdminMessage.receiver_id == admin_id)
    )
    if unread_only:
        query = query.filter(AdminMessage.is_read.is_(None))
    
    query = query.order_by(AdminMessage.created_at.desc())
    # Paginate in SQL. The caller used to fetch every message the admin had ever
    # received and slice the list in Python.
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def get_sent_messages(
    admin_id: uuid.UUID,
    db: Optional[Session] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[AdminMessage]:
    """Get messages sent by an admin (sent items)."""
    if not db:
        return []
    
    query = (
        db.query(AdminMessage)
        .options(joinedload(AdminMessage.sender), joinedload(AdminMessage.receiver))
        .filter(AdminMessage.sender_id == admin_id)
        .order_by(AdminMessage.created_at.desc())
    )
    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def count_inbox(admin_id: uuid.UUID, db: Session, unread_only: bool = False) -> int:
    """Count an admin's received messages, for pagination totals."""
    query = db.query(func.count(AdminMessage.id)).filter(
        AdminMessage.receiver_id == admin_id
    )
    if unread_only:
        query = query.filter(AdminMessage.is_read.is_(None))
    return query.scalar() or 0


def count_sent(admin_id: uuid.UUID, db: Session) -> int:
    """Count an admin's sent messages, for pagination totals."""
    return (
        db.query(func.count(AdminMessage.id))
        .filter(AdminMessage.sender_id == admin_id)
        .scalar()
        or 0
    )


def mark_read(message_id: uuid.UUID, db: Optional[Session] = None) -> AdminMessage:
    """Mark a message as read."""
    if not db:
        raise ValueError("Database session required")
    
    message = db.query(AdminMessage).filter(AdminMessage.id == message_id).first()
    if message:
        message.is_read = now_utc()
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
