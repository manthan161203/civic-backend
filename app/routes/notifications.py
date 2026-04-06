"""
Notification Routes
===================
Endpoints for managing user notifications (list, read, delete).

Frontend Integration Notes:
- Notifications are created automatically by the system (task assignment, status changes, etc.).
- Use ``unread_only=true`` to fetch only unread notifications for badge counts.
- Mark notifications as read individually or all at once.
- Notifications are user-scoped — each user only sees their own.
"""

import uuid
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.core.logger import get_logger
from app.database import get_db
from app.models.notification import Notification
from app.models.user import User

logger = get_logger("notifications")

router = APIRouter(prefix="/me/notifications", tags=["Notifications"])


class NotificationResponse(BaseModel):
    """Individual notification object.

    Attributes:
        id:           Notification UUID.
        issue_id:     Related issue UUID (null for system-wide notifications).
        title:        Notification title (localized).
        body:         Notification body text (localized).
        type:         Category — ``"status_update"`` | ``"assignment"`` | ``"resolution"`` | ``"system"``.
        is_read:      Whether the notification has been read.
        location_lat: Latitude of an attached map pin (null if not present).
        location_lng: Longitude of an attached map pin (null if not present).
        created_at:   When the notification was created.
    """

    id: uuid.UUID
    issue_id: uuid.UUID | None = None
    title: str
    body: str
    type: str
    is_read: bool
    action_type: str | None = None
    location_lat: float | None = None
    location_lng: float | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("", response_model=List[NotificationResponse])
def get_notifications(
    unread_only: bool = Query(False, description="Set true to fetch only unread notifications"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(30, ge=1, le=100, description="Items per page"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Fetch notifications for the current user, newest first.

    Use ``unread_only=true`` for badge count logic on the frontend.

    Returns:
        Paginated list of ``NotificationResponse`` objects.

    Raises:
        401: Not authenticated.
    """
    try:
        query = db.query(Notification).filter(Notification.user_id == current_user.id)
        if unread_only:
            query = query.filter(Notification.is_read == False)

        items = (
            query.order_by(Notification.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
            .all()
        )
    except Exception as e:
        logger.error(f"Error fetching notifications for user {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch notifications.",
        )

    return [NotificationResponse.model_validate(n) for n in items]


@router.post("/read-all", response_model=dict)
def mark_all_read(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark all unread notifications as read.

    Returns:
        ``{"marked_read": <count>}`` — number of notifications that were marked.

    Raises:
        401: Not authenticated.
    """
    try:
        updated = (
            db.query(Notification)
            .filter(Notification.user_id == current_user.id, Notification.is_read == False)
            .update({"is_read": True})
        )
        db.commit()
    except Exception as e:
        logger.error(f"Error marking all notifications read for user {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to mark notifications as read.",
        )

    logger.info(f"Marked {updated} notifications as read for user {current_user.id}")
    return {"marked_read": updated}


@router.post("/{notification_id}/read", response_model=dict)
def mark_one_read(
    notification_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark a single notification as read.

    No-op if the notification doesn't exist or is already read.

    Returns:
        ``{"ok": true}``

    Raises:
        401: Not authenticated.
    """
    try:
        notif = db.query(Notification).filter(
            Notification.id == notification_id,
            Notification.user_id == current_user.id,
        ).first()
        if notif:
            notif.is_read = True
            db.commit()
    except Exception as e:
        logger.error(f"Error marking notification {notification_id} as read: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to mark notification as read.",
        )

    return {"ok": True}


@router.delete("/{notification_id}", response_model=dict)
def delete_notification(
    notification_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a single notification.

    Returns:
        ``{"deleted": true}``

    Raises:
        401: Not authenticated.
        404: Notification not found (wrong ID or belongs to another user).
    """
    try:
        deleted = db.query(Notification).filter(
            Notification.id == notification_id,
            Notification.user_id == current_user.id,
        ).delete()
        db.commit()
    except Exception as e:
        logger.error(f"Error deleting notification {notification_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete notification.",
        )

    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    return {"deleted": True}


@router.delete("", response_model=dict)
def delete_all_notifications(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete all notifications for the current user.

    Returns:
        ``{"deleted": <count>}`` — number of notifications deleted.

    Raises:
        401: Not authenticated.
    """
    try:
        count = db.query(Notification).filter(
            Notification.user_id == current_user.id
        ).delete()
        db.commit()
    except Exception as e:
        logger.error(f"Error deleting all notifications for user {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete notifications.",
        )

    logger.info(f"Deleted {count} notifications for user {current_user.id}")
    return {"deleted": count}


@router.get("/count", response_model=dict)
def get_notification_count(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get notification counts for badge display.

    Returns unread count and total count in a single lightweight query.
    Prefer this over fetching all notifications just to show a badge number.

    Returns:
        ``{"unread": int, "total": int}``

    Raises:
        401: Not authenticated.
    """
    from sqlalchemy import case
    try:
        total, unread = db.query(
            func.count(Notification.id),
            func.sum(case((Notification.is_read == False, 1), else_=0)),
        ).filter(Notification.user_id == current_user.id).first()
    except Exception as e:
        logger.error(f"Error fetching notification count for user {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch notification count.",
        )

    return {"unread": int(unread or 0), "total": int(total or 0)}
