"""
Worker Assignment Management Utilities
======================================

MEDIUM PRIORITY BUG FIX #2: Worker Assignment Timeout Handling
MEDIUM PRIORITY BUG FIX #8: Badge Award Idempotency

Handles timeout checks and background job processing for worker assignments.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import and_

import logging

logger = logging.getLogger(__name__)


def check_worker_assignment_timeout(
    db: Session,
    timeout_minutes: int = 30
) -> list:
    """
    Check for issues that have been queued longer than timeout window.
    
    Returns list of issue IDs that exceeded timeout.
    
    Args:
        db: Database session
        timeout_minutes: Timeout window in minutes (default 30)
        
    Returns:
        List of issue UUIDs that timed out
        
    Note:
        This should be called by a background job scheduler (Celery, APScheduler, etc.)
        to identify and handle stale assignments.
    """
    try:
        # Import here to avoid circular imports
        from app.models.issue import Issue
        
        timeout_threshold = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
        
        # Find all queued issues that haven't been assigned
        timed_out = db.query(Issue.id).filter(
            and_(
                Issue.status == "queued",
                Issue.queued_at.isnot(None),
                Issue.queued_at < timeout_threshold
            )
        ).all()
        
        return [row[0] for row in timed_out]
        
    except Exception as e:
        logger.error(f"Error checking assignment timeouts: {str(e)}")
        return []


def handle_assignment_timeout(
    db: Session,
    issue_id: UUID,
    notify_assignee: bool = True
) -> bool:
    """
    Handle a timed-out issue assignment.
    
    Transitions issue back to "open" status.
    
    Args:
        db: Database session
        issue_id: Issue UUID that timed out
        notify_assignee: Whether to notify the reporter
        
    Returns:
        True if handled successfully, False otherwise
    """
    try:
        from app.models.issue import Issue
        from app.services.notification_service import send_notification
        
        issue = db.query(Issue).filter(Issue.id == issue_id).first()
        if not issue:
            return False
        
        # Revert to "open" status if still queued
        if issue.status == "queued":
            issue.status = "open"
            issue.queued_at = None
            db.commit()
            
            logger.info(f"Issue {issue_id} reverted from queued to open due to timeout")
            
            # Notify reporter if configured
            if notify_assignee and issue.reporter_id:
                try:
                    send_notification(
                        db,
                        user_id=issue.reporter_id,
                        title="Assignment Timeout",
                        body=f"Your issue #{issue.id} could not be assigned to a worker. "
                             f"Please try again or contact support.",
                        issue_id=issue_id
                    )
                except Exception as e:
                    logger.warning(f"Failed to notify reporter: {str(e)}")
            
            return True
        
        return False
        
    except Exception as e:
        logger.error(f"Error handling assignment timeout for {issue_id}: {str(e)}")
        return False


def process_worker_assignment_timeouts(db: Session, timeout_minutes: int = 30) -> dict:
    """
    Background job: Process all timed-out worker assignments.
    
    Arguments:
        db: Database session
        timeout_minutes: Timeout window in minutes
        
    Returns:
        Dict with 'processed' and 'failed' counts
        
    Usage:
        # In a scheduled task (e.g., Celery beat, APScheduler)
        from app.services.worker_utils import process_worker_assignment_timeouts
        
        result = process_worker_assignment_timeouts(db)
        logger.info(f"Processed {result['processed']} timed-out assignments")
    """
    timed_out_ids = check_worker_assignment_timeout(db, timeout_minutes)
    
    processed = 0
    failed = 0
    
    for issue_id in timed_out_ids:
        if handle_assignment_timeout(db, issue_id):
            processed += 1
        else:
            failed += 1
    
    logger.info(f"Worker timeout processing: {processed} handled, {failed} failed")
    
    return {
        "processed": processed,
        "failed": failed,
        "total": len(timed_out_ids)
    }


# MEDIUM PRIORITY BUG FIX #8: Badge award idempotency helpers

def award_badge_idempotent(
    db: Session,
    user_id: UUID,
    badge_slug: str,
    reference_id: Optional[str] = None
) -> bool:
    """
    Award a badge to a user with idempotency guarantee.
    
    Prevents duplicate badge awards within 5-second window.
    
    Args:
        db: Database session
        user_id: User to award badge to
        badge_slug: Badge identifier
        reference_id: Optional unique reference for dedup (e.g., task_id)
        
    Returns:
        True if badge was awarded, False if duplicate (already awarded in window)
        
    Note:
        Uses database row locks (SELECT FOR UPDATE) to prevent race conditions.
    """
    try:
        from app.models.reward import Badge, UserBadge
        
        # Generate reference_id if not provided
        if not reference_id:
            reference_id = f"{user_id}_{badge_slug}_{int(datetime.now().timestamp())}"
        
        # Check if badge was already awarded in recent window (5 seconds)
        recent_cutoff = datetime.now(timezone.utc) - timedelta(seconds=5)
        
        existing = db.query(UserBadge).filter(
            and_(
                UserBadge.user_id == user_id,
                UserBadge.badge_slug == badge_slug,
                UserBadge.awarded_at > recent_cutoff,
                UserBadge.reference_id == reference_id
            )
        ).with_for_update().first()  # Lock the row
        
        if existing:
            return False  # Already awarded in recent window
        
        # Award the badge
        badge_record = UserBadge(
            user_id=user_id,
            badge_slug=badge_slug,
            reference_id=reference_id,
            awarded_at=datetime.now(timezone.utc)
        )
        
        db.add(badge_record)
        db.commit()
        
        logger.info(f"Badge '{badge_slug}' awarded to user {user_id}")
        return True
        
    except Exception as e:
        logger.error(f"Error awarding badge idempotently: {str(e)}")
        db.rollback()
        return False
