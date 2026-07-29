"""
Citizen Features Routes
=======================
Endpoints for dispute resolution, satisfaction surveys, bookmarks,
and custom issue types — features that enhance the citizen experience.
"""

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.time import now_utc
from app.core.deps import ADMIN_ROLES, apply_admin_scope, get_current_user, require_any_admin, require_role
from app.core.logger import get_logger
from app.database import get_db
from app.models.dispute import Dispute
from app.models.issue import Issue
from app.models.issue_bookmark import IssueBookmark
from app.models.satisfaction_survey import SatisfactionSurvey
from app.models.user import User
from app.services.notification_service import notify_localized
from app.core.exceptions import CivicException
from app.services.storage import upload_image_or_raise
from app.services.utils import get_issue_or_404

logger = get_logger("citizen_features")

router = APIRouter(tags=["Citizen Features"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class DisputeCreate(BaseModel):
    reason: str = Field(..., min_length=10, max_length=2000, description="Why you believe the issue was not properly resolved")


class DisputeResponse(BaseModel):
    id: uuid.UUID
    issue_id: uuid.UUID
    citizen_id: uuid.UUID
    reason: str
    photos: List[str]
    status: str
    admin_notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DisputeResolve(BaseModel):
    outcome: str = Field(..., description="accepted | rejected")
    admin_notes: Optional[str] = Field(None, max_length=2000)


class SurveyCreate(BaseModel):
    fully_resolved: bool = Field(..., description="Was the issue fully resolved?")
    speed_rating: int = Field(..., ge=1, le=3, description="1=too_slow, 2=acceptable, 3=fast")
    would_report_again: bool = Field(..., description="Would you report again?")
    feedback: Optional[str] = Field(None, max_length=2000, description="Optional free-text feedback")


class SurveyResponse(BaseModel):
    id: uuid.UUID
    issue_id: uuid.UUID
    fully_resolved: bool
    speed_rating: int
    would_report_again: bool
    feedback: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class BookmarkResponse(BaseModel):
    id: uuid.UUID
    issue_id: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True}


class CustomIssueTypeResponse(BaseModel):
    id: uuid.UUID
    label: str
    slug: str
    usage_count: int
    is_approved: bool
    suggested_by: uuid.UUID | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


def _scoped_issue_ids(current_user: User, db: Session):
    """Issue IDs this admin may act on, or None for an unrestricted super-admin.

    Disputes and surveys are not geographic themselves but hang off an issue
    that is. Filtering on this gives them the same jurisdiction boundary the
    issue list already has — they previously had none at all, so a single-ward
    admin could read every citizen's survey feedback in the state and resolve
    any dispute anywhere.
    """
    if current_user.role == "admin":
        return None
    return apply_admin_scope(db.query(Issue.id), current_user, Issue).scalar_subquery()


# ── Disputes (#1) ────────────────────────────────────────────────────────────

@router.post("/issues/{issue_id}/dispute", response_model=DisputeResponse, status_code=201)
def create_dispute(
    issue_id: uuid.UUID,
    body: DisputeCreate,
    current_user: User = Depends(require_role("citizen", "admin")),
    db: Session = Depends(get_db),
):
    """Dispute the resolution of an issue.

    Only the reporter can dispute, and only when the issue is resolved.
    Creates a dispute record and notifies admins for review.

    **Roles**: citizen, admin.
    """
    issue = get_issue_or_404(issue_id, db)

    if str(issue.reporter_id) != str(current_user.id) and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only the reporter can dispute this issue")

    if issue.status not in ("resolved", "closed"):
        raise HTTPException(status_code=400, detail="Can only dispute resolved or closed issues")

    existing = db.query(Dispute).filter(
        Dispute.issue_id == issue_id,
        Dispute.status.in_(["open", "under_review"]),
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="An active dispute already exists for this issue")

    try:
        dispute = Dispute(
            issue_id=issue_id,
            citizen_id=current_user.id,
            reason=body.reason,
        )
        db.add(dispute)

        # Reopen the issue for re-examination
        issue.status = "open"
        issue.resolved_at = None
        db.commit()
        db.refresh(dispute)

        # Notify admins (batch notifications instead of per-admin loop)
        admins = db.query(User).filter(
            User.role.in_(["admin", "ward_admin", "taluka_admin", "district_admin"]),
            User.is_active == True,
        ).all()
        
        # Build notification payload once, send to all admins
        notification_payload = {
            "key": "dispute_opened",
            "notification_type": "system",
            "issue_id": str(issue.id),
            "action_type": "open_dispute",
            "issue_id_short": str(issue.id)[:8],
            "issue_type": issue.issue_type,
            "ward": issue.ward or "unknown",
        }
        admin_ids = [admin.id for admin in admins]
        
        # Send notification to all admins in one operation
        try:
            from app.services.notification_service import notify_localized_batch
            notify_localized_batch(db=db, user_ids=admin_ids, **notification_payload)
        except ImportError:
            # Fallback: notify_localized_batch not available, use per-admin approach
            for admin_user in admins:
                notify_localized(
                    db=db, user=admin_user, **notification_payload
                )
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error creating dispute for issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create dispute.")

    logger.info(f"Dispute created for issue {issue_id} by citizen {current_user.id}")
    return DisputeResponse.model_validate(dispute)


@router.post("/issues/{issue_id}/dispute/photos", response_model=DisputeResponse)
async def upload_dispute_photos(
    issue_id: uuid.UUID,
    photos: List[UploadFile] = File(...),
    current_user: User = Depends(require_role("citizen", "admin")),
    db: Session = Depends(get_db),
):
    """Upload evidence photos for an active dispute."""
    dispute = db.query(Dispute).filter(
        Dispute.issue_id == issue_id,
        Dispute.citizen_id == current_user.id,
        Dispute.status.in_(["open", "under_review"]),
    ).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="No active dispute found for this issue")

    try:
        uploaded_urls = []
        for photo in photos:
            file_bytes = await photo.read()
            if len(file_bytes) > 10 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="File too large. Max 10 MB.")
            filename = f"dispute_{uuid.uuid4().hex[:8]}"
            url = upload_image_or_raise(
                file_bytes, filename, user_id=str(current_user.id), issue_id=str(issue_id)
            )
            uploaded_urls.append(url)

        dispute.photos = (dispute.photos or []) + uploaded_urls
        db.commit()
        db.refresh(dispute)
    except (HTTPException, CivicException):
        # CivicException carries its own status (503 for a storage outage) and
        # is rendered by the handler in main.py — do not flatten it to a 500.
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error uploading dispute photos: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Photo upload failed.")

    return DisputeResponse.model_validate(dispute)


@router.get("/issues/{issue_id}/disputes", response_model=List[DisputeResponse])
def get_issue_disputes(
    issue_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get all disputes for an issue."""
    issue = get_issue_or_404(issue_id, db)

    if current_user.role == "citizen" and str(issue.reporter_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="You can only view disputes for your own issues")

    disputes = db.query(Dispute).filter(Dispute.issue_id == issue_id).order_by(Dispute.created_at.desc()).all()
    return [DisputeResponse.model_validate(d) for d in disputes]


@router.post("/admin/disputes/{dispute_id}/resolve", response_model=DisputeResponse)
def resolve_dispute(
    dispute_id: uuid.UUID,
    body: DisputeResolve,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Resolve a dispute (accept or reject).

    If accepted, the issue stays reopened for reassignment.
    If rejected, the issue is closed back.

    **Roles**: any admin.
    """
    if body.outcome not in ("accepted", "rejected"):
        raise HTTPException(status_code=400, detail="outcome must be 'accepted' or 'rejected'")

    dispute_q = db.query(Dispute).filter(Dispute.id == dispute_id)
    allowed = _scoped_issue_ids(current_user, db)
    if allowed is not None:
        dispute_q = dispute_q.filter(Dispute.issue_id.in_(allowed))
    dispute = dispute_q.first()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")

    if dispute.status not in ("open", "under_review"):
        raise HTTPException(status_code=400, detail="Dispute is already resolved")

    try:
        dispute.status = body.outcome
        dispute.admin_notes = body.admin_notes
        dispute.resolved_by = current_user.id

        issue = db.query(Issue).filter(Issue.id == dispute.issue_id).first()
        if body.outcome == "rejected" and issue:
            issue.status = "resolved"
        # If accepted, issue stays open for reassignment

        db.commit()
        db.refresh(dispute)

        # Notify the citizen
        citizen = db.query(User).filter(User.id == dispute.citizen_id).first()
        if citizen:
            notify_localized(
                db=db, user=citizen, key="dispute_resolved",
                notification_type="system",
                issue_id=str(dispute.issue_id),
                action_type="open_issue",
                issue_id_short=str(dispute.issue_id)[:8],
                outcome=body.outcome,
            )
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error resolving dispute {dispute_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to resolve dispute.")

    logger.info(f"Admin {current_user.id} resolved dispute {dispute_id} as {body.outcome}")
    return DisputeResponse.model_validate(dispute)


@router.get("/admin/disputes", response_model=dict)
def list_disputes(
    status_filter: Optional[str] = Query(None, alias="status", description="open | under_review | accepted | rejected"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """List disputes for admin review, scoped to the admin's jurisdiction."""
    query = db.query(Dispute)
    allowed = _scoped_issue_ids(current_user, db)
    if allowed is not None:
        query = query.filter(Dispute.issue_id.in_(allowed))
    if status_filter:
        query = query.filter(Dispute.status == status_filter)
    
    # Get total count
    total = query.count()
    
    # Get paginated items
    items = query.order_by(Dispute.created_at.desc()).offset((page - 1) * size).limit(size).all()
    
    return {
        "items": [DisputeResponse.model_validate(d) for d in items],
        "total": total,
        "page": page,
        "size": size,
        "pages": (total + size - 1) // size
    }


# ── Satisfaction Survey (#3) ─────────────────────────────────────────────────

@router.post("/issues/{issue_id}/survey", response_model=SurveyResponse, status_code=201)
def submit_survey(
    issue_id: uuid.UUID,
    body: SurveyCreate,
    current_user: User = Depends(require_role("citizen", "admin")),
    db: Session = Depends(get_db),
):
    """Submit a post-resolution satisfaction survey.

    Citizens can submit one survey per resolved issue they reported.

    **Roles**: citizen, admin.
    """
    issue = get_issue_or_404(issue_id, db)

    if str(issue.reporter_id) != str(current_user.id) and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only the reporter can submit a survey")

    if issue.status not in ("resolved", "closed"):
        raise HTTPException(status_code=400, detail="Survey can only be submitted for resolved/closed issues")

    existing = db.query(SatisfactionSurvey).filter(SatisfactionSurvey.issue_id == issue_id).first()
    if existing:
        raise HTTPException(status_code=409, detail="Survey already submitted for this issue")

    try:
        survey = SatisfactionSurvey(
            issue_id=issue_id,
            citizen_id=current_user.id,
            fully_resolved=body.fully_resolved,
            speed_rating=body.speed_rating,
            would_report_again=body.would_report_again,
            feedback=body.feedback,
        )
        db.add(survey)
        db.commit()
        db.refresh(survey)
    except Exception as e:
        db.rollback()
        logger.error(f"Error submitting survey for issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to submit survey.")

    logger.info(f"Survey submitted for issue {issue_id} by citizen {current_user.id}")
    return SurveyResponse.model_validate(survey)


@router.get("/issues/{issue_id}/survey", response_model=SurveyResponse)
def get_survey(
    issue_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the satisfaction survey for an issue (if submitted).

    **Roles**: the citizen who reported the issue, the worker who resolved it,
    and admins within whose jurisdiction the issue falls.

    Surveys carry a numeric rating and free-text feedback about a named worker.
    This endpoint had no ownership check at all — only ``get_current_user`` —
    so any citizen who could guess or enumerate an issue UUID could read every
    other citizen's rating and comments.
    """
    survey = db.query(SatisfactionSurvey).filter(SatisfactionSurvey.issue_id == issue_id).first()
    if not survey:
        raise HTTPException(status_code=404, detail="No survey found for this issue")

    issue = db.query(Issue).filter(Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=404, detail="No survey found for this issue")

    if current_user.role == "citizen":
        allowed = issue.reporter_id == current_user.id
    elif current_user.role == "worker":
        allowed = issue.assigned_worker_id == current_user.id
    elif current_user.role in ADMIN_ROLES:
        allowed = bool(
            apply_admin_scope(
                db.query(Issue.id).filter(Issue.id == issue_id), current_user, Issue
            ).first()
        )
    else:
        allowed = False

    if not allowed:
        # 404 rather than 403: confirming a survey exists for someone else's
        # issue is itself the leak this endpoint had.
        raise HTTPException(status_code=404, detail="No survey found for this issue")

    return SurveyResponse.model_validate(survey)


@router.get("/me/surveys", response_model=List[SurveyResponse])
def get_my_surveys(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get satisfaction surveys submitted by the current citizen."""
    items = (
        db.query(SatisfactionSurvey)
        .filter(SatisfactionSurvey.citizen_id == current_user.id)
        .order_by(SatisfactionSurvey.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return [SurveyResponse.model_validate(s) for s in items]


@router.get("/admin/surveys/stats", response_model=dict)
def survey_stats(
    days: int = Query(30, ge=1, le=365),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Get aggregated satisfaction survey statistics with individual survey list.

    **Roles**: any admin.
    """
    from datetime import timedelta
    cutoff = now_utc() - timedelta(days=days)

    try:
        survey_q = db.query(SatisfactionSurvey).filter(
            SatisfactionSurvey.created_at >= cutoff
        )
        # Surveys carry free-text feedback and the reporting citizen's id. This
        # returned every one in the system to any admin role.
        allowed = _scoped_issue_ids(current_user, db)
        if allowed is not None:
            survey_q = survey_q.filter(SatisfactionSurvey.issue_id.in_(allowed))
        surveys = survey_q.order_by(SatisfactionSurvey.created_at.desc()).all()
        total = len(surveys)
        if total == 0:
            return {
                "total_responses": 0,
                "fully_resolved_count": 0,
                "would_report_again_count": 0,
                "avg_speed_rating": None,
                "speed_breakdown": {"1": 0, "2": 0, "3": 0},
                "recent_feedback": [],
                "all_surveys": [],
            }

        fully_resolved_count = sum(1 for s in surveys if s.fully_resolved)
        would_report_count = sum(1 for s in surveys if s.would_report_again)
        avg_speed = sum(s.speed_rating for s in surveys) / total
        speed_breakdown = {"1": 0, "2": 0, "3": 0}
        for s in surveys:
            key = str(s.speed_rating)
            if key in speed_breakdown:
                speed_breakdown[key] += 1

        recent_feedback = [
            {
                "feedback": s.feedback,
                "speed_rating": s.speed_rating,
                "fully_resolved": s.fully_resolved,
                "would_report_again": s.would_report_again,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "issue_id": str(s.issue_id),
                "citizen_id": str(s.citizen_id),
            }
            for s in surveys[:20]
        ]

        all_surveys = [
            {
                "id": str(s.id),
                "issue_id": str(s.issue_id),
                "citizen_id": str(s.citizen_id),
                "speed_rating": s.speed_rating,
                "fully_resolved": s.fully_resolved,
                "would_report_again": s.would_report_again,
                "feedback": s.feedback,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in surveys
        ]

        return {
            "total_responses": total,
            "fully_resolved_count": fully_resolved_count,
            "would_report_again_count": would_report_count,
            "avg_speed_rating": round(avg_speed, 2),
            "speed_breakdown": speed_breakdown,
            "recent_feedback": recent_feedback,
            "all_surveys": all_surveys,
        }
    except Exception as e:
        logger.error(f"Error fetching survey stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch survey stats.")


# ── Bookmarks (#8) ───────────────────────────────────────────────────────────

@router.post("/issues/{issue_id}/bookmark", response_model=BookmarkResponse, status_code=201)
def bookmark_issue(
    issue_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Bookmark an issue to receive status updates."""
    # Called for the 404 it raises, not for the object.
    get_issue_or_404(issue_id, db)

    existing = db.query(IssueBookmark).filter(
        IssueBookmark.issue_id == issue_id,
        IssueBookmark.user_id == current_user.id,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Issue already bookmarked")

    try:
        bookmark = IssueBookmark(issue_id=issue_id, user_id=current_user.id)
        db.add(bookmark)
        db.commit()
        db.refresh(bookmark)
    except Exception as e:
        db.rollback()
        logger.error(f"Error bookmarking issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to bookmark issue.")

    return BookmarkResponse.model_validate(bookmark)


@router.delete("/issues/{issue_id}/bookmark", response_model=dict)
def remove_bookmark(
    issue_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a bookmark from an issue."""
    deleted = db.query(IssueBookmark).filter(
        IssueBookmark.issue_id == issue_id,
        IssueBookmark.user_id == current_user.id,
    ).delete()
    db.commit()

    if not deleted:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    return {"deleted": True}


@router.get("/me/bookmarks", response_model=List[BookmarkResponse])
def get_bookmarks(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get all bookmarked issues for the current user."""
    items = (
        db.query(IssueBookmark)
        .filter(IssueBookmark.user_id == current_user.id)
        .order_by(IssueBookmark.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return [BookmarkResponse.model_validate(b) for b in items]


# ── Custom Issue Types (admin management) ────────────────────────────────────

@router.get("/admin/custom-issue-types", response_model=List[CustomIssueTypeResponse])
def list_custom_issue_types(
    # Named status_filter, not status: a parameter called `status` shadows the
    # module-level `from fastapi import status`, so any HTTPException in this
    # handler using status.HTTP_* would raise AttributeError on a string.
    status_filter: Optional[str] = Query(None, alias="status"),
    approved_only: bool = Query(False),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """List citizen-suggested custom issue types, sorted by popularity.

    **Query Parameters**:
    - status: 'pending', 'approved', or 'rejected' (None = all)
    - approved_only: bool (legacy, use status='approved' instead)

    **Roles**: any admin.
    """
    from app.models.custom_issue_type import CustomIssueType
    query = db.query(CustomIssueType)
    
    # Handle status filter.
    #
    # 'rejected' is refused rather than silently answered. The model has only
    # `is_approved`, with no way to distinguish "not yet reviewed" from
    # "reviewed and turned down" — so this used to map 'rejected' onto the same
    # predicate as 'pending' and return a list of pending types labelled as
    # rejections. Returning the wrong data is worse than saying no.
    if status_filter:
        if status_filter == 'approved':
            query = query.filter(CustomIssueType.is_approved == True)  # noqa: E712
        elif status_filter == 'pending':
            query = query.filter(CustomIssueType.is_approved == False)  # noqa: E712
        elif status_filter == 'rejected':
            raise HTTPException(
                status_code=400,
                detail="Rejection is not tracked separately from pending. "
                       "Use status=pending.",
            )
        else:
            raise HTTPException(
                status_code=400,
                detail="status must be one of: approved, pending",
            )
    
    # Legacy support
    if approved_only:
        query = query.filter(CustomIssueType.is_approved == True)
    
    items = query.order_by(CustomIssueType.usage_count.desc()).all()
    return [CustomIssueTypeResponse.model_validate(i) for i in items]


@router.post("/admin/custom-issue-types/{type_id}/approve", response_model=CustomIssueTypeResponse)
def approve_custom_issue_type(
    type_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Approve a custom issue type so it appears in suggestions.

    **Roles**: any admin.
    """
    from app.models.custom_issue_type import CustomIssueType
    cit = db.query(CustomIssueType).filter(CustomIssueType.id == type_id).first()
    if not cit:
        raise HTTPException(status_code=404, detail="Custom issue type not found")

    cit.is_approved = True
    db.commit()
    db.refresh(cit)
    return CustomIssueTypeResponse.model_validate(cit)


@router.get("/issues/custom-types", response_model=List[CustomIssueTypeResponse])
def get_approved_custom_types(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get all approved custom issue types for citizens to choose from."""
    from app.models.custom_issue_type import CustomIssueType
    items = (
        db.query(CustomIssueType)
        .filter(CustomIssueType.is_approved == True)
        .order_by(CustomIssueType.usage_count.desc())
        .all()
    )
    return [CustomIssueTypeResponse.model_validate(i) for i in items]
