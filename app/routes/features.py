"""
Feature Routes — Upvotes, Flags, Search, Availability, Shifts, Subscriptions
=============================================================================
New citizen and worker feature endpoints:

Citizens:
  POST   /issues/{id}/upvote          — upvote an issue
  DELETE /issues/{id}/upvote          — remove upvote
  POST   /issues/{id}/flag            — flag an issue/comment
  GET    /issues/search               — full-text keyword search
  GET    /me/subscriptions            — list ward subscriptions
  POST   /me/subscriptions            — subscribe to a ward
  DELETE /me/subscriptions/{ward_id}  — unsubscribe from a ward

Workers:
  PUT    /workers/availability        — toggle availability (accepting tasks)
  GET    /workers/shifts              — list my shifts
  POST   /workers/shifts              — create/update a shift
  DELETE /workers/shifts/{day}        — remove a shift day

Public:
  GET    /announcements               — list active announcements for a ward/taluka/district
"""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, require_min_role, require_role
from app.core.logger import get_logger
from app.database import get_db
from app.models.announcement import Announcement
from app.services.utils import apply_not_deleted_filter, apply_pagination
from app.models.issue import Issue
from app.models.issue_flag import IssueFlag
from app.models.issue_vote import IssueVote
from app.models.location import Ward
from app.models.user import User
from app.models.ward_subscription import WardSubscription
from app.models.worker_shift import WorkerShift

logger = get_logger("features")

router = APIRouter(tags=["Features"])


# ── Issue upvotes ─────────────────────────────────────────────────────────────

@router.post("/issues/{issue_id}/upvote", status_code=status.HTTP_201_CREATED)
def upvote_issue(
    issue_id: uuid.UUID,
    current_user: User = Depends(require_role("citizen", "worker", "ward_admin", "taluka_admin", "district_admin", "admin")),
    db: Session = Depends(get_db),
):
    """Upvote/endorse an issue.

    Increments the issue's ``upvote_count``. Each user can only vote once per issue.

    **Roles**: any authenticated user.

    Raises:
        404: Issue not found.
        409: Already voted.
    """
    issue = db.query(Issue).filter(Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    try:
        vote = IssueVote(id=uuid.uuid4(), issue_id=issue_id, user_id=current_user.id)
        db.add(vote)
        db.flush()  # Discover IntegrityError early before updating count
        # Atomic SQL-level increment — safe under concurrent requests
        db.query(Issue).filter(Issue.id == issue_id).update(
            {"upvote_count": Issue.upvote_count + 1}
        )
        db.commit()
        db.refresh(issue)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="You have already upvoted this issue")
    except Exception as e:
        db.rollback()
        logger.error(f"Error upvoting issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to upvote issue.")

    logger.info(f"User {current_user.id} upvoted issue {issue_id} (total={issue.upvote_count})")

    # Reward the issue reporter for receiving a vote
    try:
        from app.services.rewards_service import award_event
        award_event(db, issue.reporter_id, "vote_received", reference_id=issue.id,
                    note=f"Issue received an upvote (total={issue.upvote_count})")
    except Exception as e:
        logger.error(f"Failed to reward vote: {e}")

    # Auto-escalate if issue crosses upvote threshold and is not yet escalated
    UPVOTE_ESCALATION_THRESHOLD = 10
    if (
        issue.upvote_count >= UPVOTE_ESCALATION_THRESHOLD
        and not issue.is_escalated
        and issue.status not in ("resolved", "closed")
    ):
        try:
            from app.services.notification_service import notify_localized
            issue.is_escalated = True
            issue.escalated_at = datetime.utcnow()
            db.commit()
            admins = db.query(User).filter(User.role == "admin", User.is_active == True).all()
            for admin_user in admins:
                notify_localized(
                    db=db,
                    user=admin_user,
                    key="escalation",
                    notification_type="system",
                    issue_id=str(issue.id),
                    issue_id_short=str(issue.id)[:8],
                    issue_type=issue.issue_type,
                    ward=issue.ward or "unknown",
                )
            logger.info(
                f"Issue {issue_id} auto-escalated via upvote threshold "
                f"({issue.upvote_count} votes ≥ {UPVOTE_ESCALATION_THRESHOLD})"
            )
        except Exception as e:
            logger.warning(f"Upvote escalation failed (non-fatal): {e}")

    return {"issue_id": str(issue_id), "upvote_count": issue.upvote_count}


@router.delete("/issues/{issue_id}/upvote", status_code=status.HTTP_200_OK)
def remove_upvote(
    issue_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove your upvote from an issue.

    **Roles**: any authenticated user.

    Raises:
        404: Issue not found or not voted.
    """
    vote = db.query(IssueVote).filter(
        IssueVote.issue_id == issue_id,
        IssueVote.user_id == current_user.id,
    ).first()
    if not vote:
        raise HTTPException(status_code=404, detail="Vote not found")

    issue = db.query(Issue).filter(Issue.id == issue_id).first()
    try:
        db.delete(vote)
        if issue and issue.upvote_count and issue.upvote_count > 0:
            issue.upvote_count -= 1
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error removing upvote: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to remove upvote.")

    return {"issue_id": str(issue_id), "upvote_count": issue.upvote_count if issue else 0}


# ── Issue flags / reports ─────────────────────────────────────────────────────

class FlagRequest(BaseModel):
    """Request body for flagging an issue or comment."""
    reason: str = Field(..., description="spam | inappropriate | duplicate | false_report | other")
    details: Optional[str] = Field(None, max_length=1000, description="Optional additional details")
    comment_id: Optional[uuid.UUID] = Field(None, description="Set to flag a comment instead of the issue")


@router.post("/issues/{issue_id}/flag", status_code=status.HTTP_201_CREATED)
def flag_issue(
    issue_id: uuid.UUID,
    body: FlagRequest,
    current_user: User = Depends(require_role("citizen", "worker", "ward_admin", "taluka_admin", "district_admin", "admin")),
    db: Session = Depends(get_db),
):
    """Flag an issue or one of its comments for admin review.

    **Roles**: any authenticated user.

    Raises:
        404: Issue not found.
        400: Invalid reason.
    """
    valid_reasons = {"spam", "inappropriate", "duplicate", "false_report", "other"}
    if body.reason not in valid_reasons:
        raise HTTPException(status_code=400, detail=f"reason must be one of: {', '.join(valid_reasons)}")

    issue = db.query(Issue).filter(Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    try:
        flag = IssueFlag(
            id=uuid.uuid4(),
            reporter_id=current_user.id,
            issue_id=issue_id if not body.comment_id else None,
            comment_id=body.comment_id,
            reason=body.reason,
            details=body.details,
            status="pending",
        )
        db.add(flag)
        db.commit()
        db.refresh(flag)
    except Exception as e:
        db.rollback()
        logger.error(f"Error flagging issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to submit flag.")

    logger.info(f"User {current_user.id} flagged issue {issue_id} (reason={body.reason})")
    return {"id": str(flag.id), "status": flag.status, "message": "Report submitted. Our team will review it."}


# ── Issue search ──────────────────────────────────────────────────────────────

@router.get("/issues/search")
def search_issues(
    q: str = Query(..., min_length=2, max_length=200, description="Search keyword"),
    issue_type: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    ward: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Full-text keyword search across issue descriptions and addresses.

    Searches ``description`` and ``address`` fields (case-insensitive).
    Results are ordered by upvote count (descending), then creation date.

    **Roles**: any authenticated user.
    """
    try:
        query = db.query(Issue)
        query = apply_not_deleted_filter(query)

        # Role-based visibility
        if current_user.role == "citizen":
            query = query.filter(Issue.reporter_id == current_user.id)
        elif current_user.role == "worker":
            query = query.filter(Issue.assigned_worker_id == current_user.id)

        like = f"%{q}%"
        query = query.filter(
            or_(
                Issue.description.ilike(like),
                Issue.address.ilike(like),
                Issue.ward.ilike(like),
            )
        )

        if issue_type:
            query = query.filter(Issue.issue_type == issue_type)
        if status_filter:
            query = query.filter(Issue.status == status_filter)
        if ward:
            query = query.filter(Issue.ward == ward)
        if priority:
            query = query.filter(Issue.priority == priority)

        total = query.count()
        items = (
            query
            .order_by(Issue.upvote_count.desc(), Issue.created_at.desc())
        )
        items = apply_pagination(items, page, size).all()
    except Exception as e:
        logger.error(f"Error searching issues: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Search failed.")

    from app.schemas.issue import IssueResponse
    return {
        "query": q,
        "total": total,
        "page": page,
        "size": size,
        "items": [IssueResponse.model_validate(i).model_dump() for i in items],
    }


# ── Worker availability toggle ────────────────────────────────────────────────

class AvailabilityUpdate(BaseModel):
    is_available: bool = Field(..., description="True = accepting new assignments, False = unavailable")


@router.put("/workers/availability")
def update_availability(
    body: AvailabilityUpdate,
    current_user: User = Depends(require_role("worker")),
    db: Session = Depends(get_db),
):
    """Toggle worker availability for new task assignments.

    This is separate from ``is_online`` (app open/connected).
    ``is_available=false`` means the worker will be skipped during geo-routing
    even if they are online (e.g. on a break, at lunch).

    **Roles**: worker only.
    """
    try:
        current_user.is_available = body.is_available
        db.commit()
    except Exception as e:
        logger.error(f"Error updating availability for worker {current_user.id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update availability.")

    logger.info(f"Worker {current_user.id} availability → {body.is_available}")
    return {
        "worker_id": str(current_user.id),
        "is_available": current_user.is_available,
        "is_online": current_user.is_online,
    }


# ── Worker shifts ─────────────────────────────────────────────────────────────

class ShiftUpsert(BaseModel):
    """Create or update a shift for a specific day."""
    day_of_week: int = Field(..., ge=0, le=6, description="0=Monday … 6=Sunday")
    start_time: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="HH:MM in 24h format")
    end_time: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="HH:MM in 24h format")


@router.get("/workers/shifts")
def list_shifts(
    current_user: User = Depends(require_role("worker")),
    db: Session = Depends(get_db),
):
    """List my weekly shift schedule.

    **Roles**: worker only.
    """
    shifts = (
        db.query(WorkerShift)
        .filter(WorkerShift.worker_id == current_user.id, WorkerShift.is_active == True)
        .order_by(WorkerShift.day_of_week)
        .all()
    )
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return [
        {
            "id": str(s.id),
            "day_of_week": s.day_of_week,
            "day_name": days[s.day_of_week],
            "start_time": s.start_time,
            "end_time": s.end_time,
        }
        for s in shifts
    ]


@router.post("/workers/shifts", status_code=status.HTTP_201_CREATED)
def upsert_shift(
    body: ShiftUpsert,
    current_user: User = Depends(require_role("worker")),
    db: Session = Depends(get_db),
):
    """Create or update the shift for a given day.

    If a shift already exists for that day, it is updated (upsert).

    **Roles**: worker only.
    """
    if body.start_time >= body.end_time:
        raise HTTPException(status_code=400, detail="start_time must be before end_time")

    try:
        existing = db.query(WorkerShift).filter(
            WorkerShift.worker_id == current_user.id,
            WorkerShift.day_of_week == body.day_of_week,
        ).first()

        if existing:
            existing.start_time = body.start_time
            existing.end_time = body.end_time
            existing.is_active = True
            shift = existing
        else:
            shift = WorkerShift(
                id=uuid.uuid4(),
                worker_id=current_user.id,
                day_of_week=body.day_of_week,
                start_time=body.start_time,
                end_time=body.end_time,
            )
            db.add(shift)

        db.commit()
        db.refresh(shift)
    except Exception as e:
        db.rollback()
        logger.error(f"Error upserting shift: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to save shift.")

    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return {
        "id": str(shift.id),
        "day_of_week": shift.day_of_week,
        "day_name": days[shift.day_of_week],
        "start_time": shift.start_time,
        "end_time": shift.end_time,
    }


@router.delete("/workers/shifts/{day_of_week}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shift(
    day_of_week: int,
    current_user: User = Depends(require_role("worker")),
    db: Session = Depends(get_db),
):
    """Remove shift for a specific day (0=Monday … 6=Sunday).

    **Roles**: worker only.
    """
    if day_of_week < 0 or day_of_week > 6:
        raise HTTPException(status_code=400, detail="day_of_week must be 0-6")
    shift = db.query(WorkerShift).filter(
        WorkerShift.worker_id == current_user.id,
        WorkerShift.day_of_week == day_of_week,
    ).first()
    if not shift:
        raise HTTPException(status_code=404, detail="Shift not found for that day")
    db.delete(shift)
    db.commit()


# ── Ward subscriptions ────────────────────────────────────────────────────────

@router.get("/me/subscriptions")
def list_subscriptions(
    current_user: User = Depends(require_role("citizen")),
    db: Session = Depends(get_db),
):
    """List all wards I am subscribed to for issue notifications.

    **Roles**: citizen only.
    """
    subs = db.query(WardSubscription).filter(WardSubscription.user_id == current_user.id).all()
    result = []
    for s in subs:
        ward = db.query(Ward).filter(Ward.id == s.ward_id).first()
        result.append({
            "ward_id": str(s.ward_id),
            "ward_name": ward.name if ward else None,
            "ward_number": ward.ward_number if ward else None,
            "subscribed_at": s.created_at.isoformat() if s.created_at else None,
        })
    return result


class SubscribeRequest(BaseModel):
    ward_id: uuid.UUID = Field(..., description="UUID of the ward to subscribe to")


@router.post("/me/subscriptions", status_code=status.HTTP_201_CREATED)
def subscribe_to_ward(
    body: SubscribeRequest,
    current_user: User = Depends(require_role("citizen")),
    db: Session = Depends(get_db),
):
    """Subscribe to issue notifications for a ward.

    You will receive push notifications when new issues are filed in this ward.
    Maximum 10 subscriptions per citizen.

    **Roles**: citizen only.

    Raises:
        404: Ward not found.
        409: Already subscribed.
        400: Subscription limit reached (10 wards max).
    """
    ward = db.query(Ward).filter(Ward.id == body.ward_id).first()
    if not ward:
        raise HTTPException(status_code=404, detail="Ward not found")

    count = db.query(WardSubscription).filter(WardSubscription.user_id == current_user.id).count()
    if count >= 10:
        raise HTTPException(status_code=400, detail="Maximum 10 ward subscriptions allowed")

    try:
        sub = WardSubscription(id=uuid.uuid4(), user_id=current_user.id, ward_id=body.ward_id)
        db.add(sub)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Already subscribed to this ward")
    except Exception as e:
        db.rollback()
        logger.error(f"Error subscribing to ward: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to subscribe.")

    logger.info(f"Citizen {current_user.id} subscribed to ward {body.ward_id}")
    return {"ward_id": str(body.ward_id), "ward_name": ward.name, "message": "Subscribed successfully"}


@router.delete("/me/subscriptions/{ward_id}", status_code=status.HTTP_204_NO_CONTENT)
def unsubscribe_from_ward(
    ward_id: uuid.UUID,
    current_user: User = Depends(require_role("citizen")),
    db: Session = Depends(get_db),
):
    """Unsubscribe from issue notifications for a ward.

    **Roles**: citizen only.
    """
    sub = db.query(WardSubscription).filter(
        WardSubscription.user_id == current_user.id,
        WardSubscription.ward_id == ward_id,
    ).first()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    db.delete(sub)
    db.commit()
    logger.info(f"Citizen {current_user.id} unsubscribed from ward {ward_id}")


# ── Public announcements ──────────────────────────────────────────────────────

@router.get("/announcements")
def list_public_announcements(
    ward_id: Optional[uuid.UUID] = Query(None, description="Filter by ward UUID"),
    taluka_id: Optional[uuid.UUID] = Query(None, description="Filter by taluka UUID"),
    district_id: Optional[uuid.UUID] = Query(None, description="Filter by district UUID"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List active announcements relevant to a citizen's location.

    Returns announcements matching the provided location parameters
    plus all state-wide (scope=state) announcements.
    Expired announcements are excluded automatically.

    **Roles**: any authenticated user.
    """
    try:
        now = datetime.utcnow()
        base = db.query(Announcement).filter(
            (Announcement.expires_at.is_(None)) | (Announcement.expires_at > now)
        )

        filters = [Announcement.scope == "state"]
        if ward_id:
            filters.append(Announcement.ward_id == ward_id)
        if taluka_id:
            filters.append(Announcement.taluka_id == taluka_id)
        if district_id:
            filters.append(Announcement.district_id == district_id)

        # Combine with OR so state-wide always appears
        from sqlalchemy import or_ as _or
        query = base.filter(_or(*filters))

        total = query.count()
        items = query.order_by(Announcement.created_at.desc())
        items = apply_pagination(items, page, size).all()
    except Exception as e:
        logger.error(f"Error listing public announcements: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch announcements.")

    return {
        "total": total, "page": page, "size": size,
        "items": [
            {
                "id": str(a.id),
                "title": a.title,
                "body": a.body,
                "scope": a.scope,
                "expires_at": a.expires_at.isoformat() if a.expires_at else None,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in items
        ],
    }
