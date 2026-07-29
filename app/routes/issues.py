"""
Issue Routes
============
Endpoints for creating, viewing, updating, and commenting on civic issues.

Frontend Integration Notes:
- Issue creation is a two-step process: ``POST /issues`` then ``POST /issues/{id}/photos``.
- AI classification happens automatically on the first before-photo upload.
- Citizens see only their own issues; workers see only their assigned tasks; admins see all.
- Status flow: open → assigned → in_progress → resolved → closed.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func
from sqlalchemy.orm import Session


from app.core.time import now_utc
from app.core.config import settings
from app.core.deps import ADMIN_ROLES, apply_admin_scope, get_current_user, require_role
from app.core.logger import get_logger
from app.core.exceptions import (
    ValidationError, ResourceNotFoundError, AuthorizationError,
    ExternalServiceError, ProcessingError, CivicException
)
from app.core.constants import (
    BUILT_IN_ISSUE_TYPES, ISSUE_SEVERITY_DEFAULT, ISSUE_PRIORITY_DEFAULT, ISSUE_PRIORITY_URGENT, ISSUE_STATUS_CLOSED,
    DAILY_ISSUE_LIMIT_CITIZEN
)
from app.database import get_db
from app.models.issue import Issue
from app.models.issue_comment import IssueComment
from app.models.issue_vote import IssueVote
from app.models.user import User
from app.schemas.issue import IssueCommentCreate, IssueCommentResponse, IssueCreate, IssueListResponse, IssueResponse, IssueUpdate
from app.services.ai_service import check_duplicate, classify_issue
from app.services.geo_service import auto_assign
from app.services.notification_service import notify, notify_localized, send_sms_status_update
from app.services.storage import upload_image_or_raise
from app.services.utils import (
    validate_photo_file, get_issue_or_404, batch_commit
)

logger = get_logger("issues")

router = APIRouter(prefix="/issues", tags=["Issues"])


# ───────────────────────────────────────────────────────────────────────────────
# Helper Functions
# ───────────────────────────────────────────────────────────────────────────────

def _get_comment_count(db: Session, issue_id: uuid.UUID) -> int:
    """Get the number of comments for a single issue."""
    count = db.query(func.count(IssueComment.id)).filter(
        IssueComment.issue_id == issue_id
    ).scalar()
    return count or 0


def _populate_comment_counts(db: Session, responses: List[IssueResponse]) -> None:
    """Populate comment_count for a list of IssueResponse objects in-place."""
    if not responses:
        return
    
    issue_ids = [r.id for r in responses]
    comment_counts = {}
    
    if issue_ids:
        rows = db.query(IssueComment.issue_id, func.count(IssueComment.id)).filter(
            IssueComment.issue_id.in_(issue_ids)
        ).group_by(IssueComment.issue_id).all()
        comment_counts = {row[0]: row[1] for row in rows}
    
    for resp in responses:
        resp.comment_count = comment_counts.get(resp.id, 0)


def _validate_status_transition(current_status: str, new_status: str, user_role: str) -> None:
    """Validate that a status transition is allowed for the given role.
    
    Valid transitions:
    - Admin: open → assigned, assigned → in_progress, in_progress → resolved, resolved → closed
    - Worker: assigned → in_progress, in_progress → resolved
    - Citizen: resolved → closed, open → open (reopen) [when is_admin=true in body]
    - Anyone: * → open (reopen if issue was resolved/closed)
    
    FIX: HIGH PRIORITY BUG #3 - Status transition validation missing
    """
    # The table below is written in terms of "admin". Every admin role carries
    # the same status-transition authority — they differ in *which issues* they
    # may touch, which is enforced by the caller's scope check, not here. Without
    # this normalization a ward_admin was rejected for every transition.
    if user_role in ADMIN_ROLES:
        user_role = "admin"

    valid_transitions = {
        ("open", "assigned"): {"admin"},
        ("open", "in_progress"): {"admin"},
        ("assigned", "in_progress"): {"worker", "admin"},
        ("in_progress", "resolved"): {"worker", "admin"},
        ("resolved", "closed"): {"citizen", "admin"},
        ("resolved", "open"): {"admin"},  # Reopen
        ("closed", "open"): {"admin"},  # Reopen
        ("open", "open"): {"citizen", "worker", "admin"},  # No-op
    }
    
    transition = (current_status, new_status)
    if transition not in valid_transitions:
        raise ValidationError(
            "invalid_status_transition",
            f"Cannot transition from '{current_status}' to '{new_status}' for role '{user_role}'. "
            f"Valid next states from '{current_status}': "
            f"{', '.join(dst for (src, dst) in valid_transitions.keys() if src == current_status)}"
        )
    
    if user_role not in valid_transitions[transition]:
        allowed_roles = valid_transitions[transition]
        raise ValidationError(
            "unauthorized_status_transition",
            f"Role '{user_role}' cannot transition to '{new_status}'. "
            f"Allowed roles: {', '.join(sorted(allowed_roles))}"
        )


@router.post("", response_model=IssueResponse, status_code=status.HTTP_201_CREATED)
async def create_issue(
    body: IssueCreate,
    current_user: User = Depends(require_role("citizen", "admin")),
    db: Session = Depends(get_db),
):
    """Create a new civic issue report.

    Automatically checks for duplicate issues within 50m in the last 48 hours.
    If no duplicate is found, auto-assigns the nearest available worker.

    **Roles**: citizen, admin.

    **Rate Limit**: Citizens can report max 10 issues per calendar day.

    Args:
        body: Issue details (type, description, location, etc.)
        current_user: Authenticated user (citizen or admin)
        db: Database session

    Returns:
        IssueResponse: Created issue with full details

    Raises:
        ValidationError: If input validation fails or daily limit exceeded
        ResourceNotFoundError: If referenced ward/custom type not found
        ProcessingError: If issue creation fails unexpectedly
    """
    try:
        from app.models.custom_issue_type import CustomIssueType
        from app.models.location import Ward as WardModel
        import re

        # ──── RATE LIMITING ─────────────────────────────────────────────────
        if current_user.role == "citizen":
            today_start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
            today_count = (
                db.query(Issue)
                .filter(
                    Issue.reporter_id == current_user.id,
                    Issue.created_at >= today_start,
                    Issue.is_deleted == False,
                )
                .count()
            )
            if today_count >= DAILY_ISSUE_LIMIT_CITIZEN:
                raise ValidationError(
                    "daily_limit",
                    f"Daily limit reached. You can report at most {DAILY_ISSUE_LIMIT_CITIZEN} "
                    f"issues per day. Please try again tomorrow."
                )

        # ──── GPS BOUNDS VALIDATION ────────────────────────────────────────────
        if not (-90 <= body.latitude <= 90):
            raise ValidationError(
                "latitude",
                "Latitude must be between -90 and 90"
            )
        if not (-180 <= body.longitude <= 180):
            raise ValidationError(
                "longitude",
                "Longitude must be between -180 and 180"
            )

        # ──── WARD RESOLUTION ────────────────────────────────────────────────
        resolved_ward = body.ward or current_user.ward
        if body.ward_id:
            ward_obj = db.query(WardModel).filter(WardModel.id == body.ward_id).first()
            if not ward_obj:
                raise ResourceNotFoundError(
                    "ward",
                    f"Ward with ID {body.ward_id} not found",
                    {"ward_id": str(body.ward_id)}
                )
            resolved_ward = ward_obj.name

        # ──── ISSUE TYPE VALIDATION ─────────────────────────────────────────
        final_issue_type = body.issue_type
        custom_label = None

        if body.issue_type not in BUILT_IN_ISSUE_TYPES:
            # Check if it's an approved custom type
            custom = db.query(CustomIssueType).filter(
                CustomIssueType.slug == body.issue_type,
                CustomIssueType.is_approved == True
            ).first()
            if not custom:
                raise ValidationError(
                    "issue_type",
                    f"Issue type '{body.issue_type}' is not approved yet or invalid. "
                    f"Built-in types: {', '.join(sorted(BUILT_IN_ISSUE_TYPES))}"
                )
            custom.usage_count += 1
            final_issue_type = custom.slug
        else:
            # Handle "other" type with custom label
            if body.issue_type == "other" and body.custom_issue_type_label:
                if not body.custom_issue_type_label.strip():
                    raise ValidationError(
                        "custom_issue_type_label",
                        "Custom type label cannot be empty when issue_type is 'other'"
                    )
                custom_label = body.custom_issue_type_label.strip()

        # ──── ISSUE CREATION ────────────────────────────────────────────────
        issue = Issue(
            reporter_id=current_user.id,
            issue_type=final_issue_type,
            custom_issue_type_label=custom_label,
            severity=body.severity or ISSUE_SEVERITY_DEFAULT,
            priority=ISSUE_PRIORITY_URGENT if body.is_sos else (body.priority or ISSUE_PRIORITY_DEFAULT),
            department=body.department,
            description=body.description,
            latitude=body.latitude,
            longitude=body.longitude,
            address=body.address,
            ward=resolved_ward,
            ward_id=body.ward_id,
            before_photos=[],
            after_photos=[],
            is_sos=body.is_sos,
        )

        # ──── TRACK CUSTOM ISSUE TYPES ──────────────────────────────────────
        if body.issue_type == "other" and body.custom_issue_type_label:
            try:
                slug = re.sub(r"[^a-z0-9]+", "_", custom_label.lower()).strip("_")
                existing = db.query(CustomIssueType).filter(
                    CustomIssueType.slug == slug
                ).first()
                if existing:
                    existing.usage_count += 1
                else:
                    db.add(CustomIssueType(
                        label=custom_label,
                        slug=slug,
                        suggested_by=current_user.id,
                    ))
            except Exception as e:
                logger.warning(
                    f"Failed to track custom issue type: {str(e)}",
                    extra={"custom_label": custom_label}
                )

        # ──── SOS ESCALATION ────────────────────────────────────────────────
        if body.is_sos:
            issue.is_escalated = True
            issue.escalated_at = now_utc()
            logger.warning(
                "SOS issue created",
                extra={
                    "issue_id": str(issue.id),
                    "reporter_id": str(current_user.id),
                    "location": f"({body.latitude},{body.longitude})"
                }
            )

        # ──── DUPLICATE DETECTION ───────────────────────────────────────────
        duplicate = None
        try:
            # AI-based duplicate detection (external service)
            duplicate = check_duplicate(body.issue_type, body.latitude, body.longitude, db)
        except Exception as e:
            logger.warning(
                "AI duplicate check failed, falling back to rule-based detection",
                extra={
                    "error": str(e),
                    "issue_type": body.issue_type,
                    "lat": body.latitude,
                    "lng": body.longitude
                }
            )

        if not duplicate:
            # Rule-based fallback: same type + same ward within 24h
            since = now_utc() - timedelta(hours=24)
            rule_duplicate = (
                db.query(Issue)
                .filter(
                    Issue.issue_type == body.issue_type,
                    Issue.is_deleted == False,
                    Issue.created_at >= since,
                    Issue.status != ISSUE_STATUS_CLOSED,
                )
            )
            if body.ward_id:
                rule_duplicate = rule_duplicate.filter(Issue.ward_id == body.ward_id)
            elif resolved_ward:
                rule_duplicate = rule_duplicate.filter(Issue.ward == resolved_ward)
            duplicate = rule_duplicate.first()

        if duplicate:
            issue.is_duplicate = True
            issue.parent_issue_id = duplicate.id
            logger.info(
                "Issue marked as duplicate",
                extra={
                    "issue_id": str(issue.id),
                    "parent_issue_id": str(duplicate.id)
                }
            )

        # ──── PERSIST ISSUE ────────────────────────────────────────────────
        db.add(issue)
        batch_commit(db, "create_issue")
        db.refresh(issue)

        # ──── AUTO-ASSIGNMENT TO WORKER ─────────────────────────────────────
        if not issue.is_duplicate:
            try:
                assigned = auto_assign(issue, db)
                if assigned and issue.assigned_worker:
                    worker = issue.assigned_worker
                    # Notify worker of new assignment
                    try:
                        notify(
                            db=db,
                            user_id=str(worker.id),
                            title="New task assigned",
                            body=f"A {issue.issue_type} issue has been assigned to you in {issue.ward or 'your area'}.",
                            notification_type="assignment",
                            issue_id=str(issue.id),
                            fcm_token=worker.fcm_token,
                            action_type="view_task",
                        )
                    except Exception as e:
                        logger.error(
                            "Failed to notify worker of assignment",
                            extra={"worker_id": str(worker.id), "error": str(e)}
                        )

                    # SMS notification to reporter
                    try:
                        send_sms_status_update(
                            phone=current_user.phone,
                            lang=getattr(current_user, "language", "en"),
                            sms_key="assigned",
                            issue_type=issue.issue_type,
                            issue_id_short=str(issue.id)[:8],
                        )
                    except Exception as e:
                        logger.error(
                            "Failed to send SMS to reporter",
                            extra={"reporter_id": str(current_user.id), "error": str(e)}
                        )

                    logger.info(
                        "Issue auto-assigned to worker",
                        extra={
                            "issue_id": str(issue.id),
                            "worker_id": str(worker.id)
                        }
                    )
            except ExternalServiceError as e:
                # Auto-assignment is non-critical, log and continue
                logger.warning(
                    "Auto-assignment service unavailable",
                    extra={"issue_id": str(issue.id), "error": str(e)}
                )

        # ──── SOS BROADCAST ─────────────────────────────────────────────────
        # FIX HIGH PRIORITY BUG #2: SOS broadcast duplicate prevention
        # Check sos_radius_notified flag to prevent duplicate broadcasts
        if body.is_sos and not issue.sos_radius_notified:
            try:
                # Notify citizens within 500m radius
                from math import radians, cos, sin, asin, sqrt

                def haversine(lat1, lon1, lat2, lon2):
                    """Calculate distance between two points in km."""
                    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
                    dlon = lon2 - lon1
                    dlat = lat2 - lat1
                    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
                    c = 2 * asin(sqrt(a))
                    r = 6371  # Radius of earth in kilometers
                    return c * r

                nearby_citizens = db.query(User).filter(
                    User.role == "citizen",
                    User.is_active == True
                ).all()

                notified = 0
                for citizen in nearby_citizens:
                    if citizen.latitude and citizen.longitude:
                        dist = haversine(
                            issue.latitude, issue.longitude,
                            citizen.latitude, citizen.longitude
                        )
                        if dist <= 0.5:  # 500m
                            try:
                                notify(
                                    db=db,
                                    user_id=str(citizen.id),
                                    title="DANGER: Hazard Near You",
                                    body=f"Critical hazard reported near {issue.address or 'your area'}. "
                                         f"Please avoid the area.",
                                    notification_type="system",
                                    issue_id=str(issue.id),
                                    fcm_token=citizen.fcm_token,
                                )
                                notified += 1
                            except Exception:
                                pass  # Non-critical

                issue.sos_radius_notified = True  # Set flag to prevent duplicate broadcasts
                db.commit()
                logger.warning(
                    "SOS broadcast completed",
                    extra={"issue_id": str(issue.id), "notified_citizens": notified}
                )

                # Notify all admins
                try:
                    admins = db.query(User).filter(
                        User.role == "admin",
                        User.is_active == True
                    ).all()
                    for admin_user in admins:
                        notify_localized(
                            db=db,
                            user=admin_user,
                            key="sos_alert",
                            notification_type="system",
                            issue_id=str(issue.id),
                            issue_id_short=str(issue.id)[:8],
                            issue_type=issue.issue_type,
                            address=issue.address or "Unknown",
                            ward=issue.ward or "unknown",
                        )
                    logger.info(
                        "SOS alert sent to admins",
                        extra={"issue_id": str(issue.id), "admin_count": len(admins)}
                    )
                except Exception as e:
                    logger.error(
                        "Failed to notify admins of SOS",
                        extra={"issue_id": str(issue.id), "error": str(e)}
                    )
            except Exception as e:
                logger.error(
                    "SOS broadcast failed",
                    extra={"issue_id": str(issue.id), "error": str(e)}
                )

        # ──── REWARDS ───────────────────────────────────────────────────────
        try:
            from app.services.rewards_service import award_event
            award_event(db, current_user.id, "report_issue", reference_id=issue.id)
        except Exception as e:
            logger.warning(
                "Failed to award points for issue report",
                extra={"reporter_id": str(current_user.id), "error": str(e)}
            )

        # ──── RESPONSE ──────────────────────────────────────────────────────
        logger.info(
            "Issue report created successfully",
            extra={
                "issue_id": str(issue.id),
                "reporter_id": str(current_user.id),
                "is_sos": body.is_sos,
                "is_duplicate": issue.is_duplicate
            }
        )
        result = IssueResponse.model_validate(issue)
        result.comment_count = _get_comment_count(db, issue.id)
        return result

    except CivicException:
        # Re-raise our custom exceptions (these have proper status codes)
        raise
    except Exception as e:
        logger.error(
            "Unexpected error creating issue",
            extra={"reporter_id": str(current_user.id), "error": str(e)},
            exc_info=True
        )
        raise ProcessingError(
            "issue_creation",
            "Failed to create issue. Please try again later."
        )

    # NOTE: 90 lines of unreachable code were removed here.
    #
    # The try block above ends with `return result` and every handler ends
    # with `raise`, so nothing after it could ever execute. The dead block was
    # a second SOS broadcast and a second `award_event("report_issue")` call —
    # both of which the live path above already performs, guarded by the
    # `sos_radius_notified` flag.
    #
    # It also contained the `with_for_update()` row lock described in the
    # comment as "FIX HIGH PRIORITY BUG #2 - Duplicate SOS broadcasts due to
    # race condition". Anyone reading this file believed SOS creation took a
    # row lock. It did not, and does not — but it does not need one: the issue
    # was created by this request microseconds earlier and no other request
    # can hold a reference to it yet.


@router.post("/{issue_id}/photos", response_model=IssueResponse)
async def upload_photos(
    issue_id: uuid.UUID,
    photos: List[UploadFile] = File(...),
    photo_type: str = Query("before", pattern="^(before|after)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload before or after photos for an issue and trigger AI classification.

    **Before photos**: Only the issue reporter can upload. AI classification runs automatically.
    **After photos**: Only the assigned worker or admin can upload. Indicates work completion.

    Args:
        issue_id: UUID of the issue to upload photos for
        photos: One or more image files (multipart/form-data)
        photo_type: Type of photo - "before" or "after" (default: "before")
        current_user: Authenticated user
        db: Database session

    Returns:
        IssueResponse: Updated issue with new photo URLs

    Raises:
        ResourceNotFoundError: If issue not found
        AuthorizationError: If user lacks permission to upload this photo type
        ValidationError: If photo file is invalid (size, type, etc.)
        ExternalServiceError: If photo upload or AI classification fails
        ProcessingError: If database operation fails
    """
    try:
        # ──── FETCH ISSUE ───────────────────────────────────────────────────
        issue = get_issue_or_404(issue_id, db)

        # ──── PERMISSION CHECKS ─────────────────────────────────────────────
        if photo_type == "before":
            # Only reporter can upload before photos
            if str(issue.reporter_id) != str(current_user.id):
                raise AuthorizationError(
                    "upload_before_photos",
                    "Only the issue reporter can upload before photos"
                )
        else:  # after
            # Only assigned worker or admin can upload after photos
            is_assigned_worker = str(issue.assigned_worker_id) == str(current_user.id)
            is_admin = current_user.role == "admin"
            if not (is_assigned_worker or is_admin):
                raise AuthorizationError(
                    "upload_after_photos",
                    "Only the assigned worker or admin can upload after photos"
                )

        # ──── VALIDATE INPUTS ───────────────────────────────────────────────
        if not photos or len(photos) == 0:
            raise ValidationError(
                "photos",
                "At least one photo is required"
            )

        # `hasattr(globals(), name)` asks whether a *dict object* has that
        # attribute, which is always False — so this was hardwired to 10 and the
        # constant it referenced was never defined anyway. Now a real setting.
        max_photos = settings.MAX_PHOTOS_PER_ISSUE
        current_photo_count = len(issue.before_photos or []) if photo_type == "before" else len(issue.after_photos or [])
        if current_photo_count + len(photos) > max_photos:
            raise ValidationError(
                "photos",
                f"Maximum {max_photos} photos allowed per issue. Current: {current_photo_count}, requesting: {len(photos)}"
            )

        # ──── PROCESS UPLOADED FILES ────────────────────────────────────────
        uploaded_urls = []
        first_bytes: Optional[bytes] = None
        first_mime: str = "image/jpeg"

        for photo_file in photos:
            try:
                # Read file content
                file_bytes = await photo_file.read()

                # Validate photo using utility function
                validate_photo_file(
                    photo_file.filename or "unknown",
                    len(file_bytes),
                    photo_file.content_type or "application/octet-stream"
                )

                # Determine MIME type
                mime_type = (photo_file.content_type or "").split(";")[0].strip().lower()
                if not mime_type:
                    mime_type = "image/jpeg"

                # Upload to storage
                try:
                    filename = f"{photo_type}_{uuid.uuid4().hex[:8]}"
                    url = upload_image_or_raise(
                        file_bytes,
                        filename,
                        user_id=str(current_user.id),
                        issue_id=str(issue_id),
                    )
                    uploaded_urls.append(url)

                    # Save first photo for AI classification
                    if first_bytes is None:
                        first_bytes = file_bytes
                        first_mime = mime_type

                    logger.info(
                        "Photo uploaded successfully",
                        extra={
                            "issue_id": str(issue_id),
                            "photo_type": photo_type,
                            "size_bytes": len(file_bytes)
                        }
                    )

                except ExternalServiceError:
                    raise  # Re-raise storage errors
                except Exception as e:
                    raise ExternalServiceError(
                        "Storage",
                        f"Failed to upload photo: {str(e)}",
                        transient=False,
                        details={"filename": photo_file.filename}
                    )

            except CivicException:
                raise  # Re-raise validation/auth errors immediately
            except Exception as e:
                logger.error(
                    "Error processing individual photo",
                    extra={
                        "issue_id": str(issue_id),
                        "filename": (photo_file.filename or "unknown"),
                        "error": str(e)
                    }
                )
                raise

        # ──── UPDATE ISSUE WITH PHOTOS ──────────────────────────────────────
        if photo_type == "before":
            issue.before_photos = (issue.before_photos or []) + uploaded_urls

            # AI Classification on first before-photo
            if first_bytes is not None:
                try:
                    ai_result = await classify_issue(first_bytes, first_mime)
                    issue.ai_issue_type = ai_result.get("issue_type")
                    issue.ai_severity = ai_result.get("severity")
                    issue.ai_confidence = ai_result.get("confidence")
                    issue.ai_suggested_description = ai_result.get("suggested_description")

                    logger.info(
                        "AI classification completed",
                        extra={
                            "issue_id": str(issue_id),
                            "ai_type": ai_result.get("issue_type"),
                            "ai_severity": ai_result.get("severity"),
                            "ai_confidence": ai_result.get("confidence")
                        }
                    )
                except Exception as e:
                    # AI classification is non-critical
                    logger.warning(
                        "AI classification failed (non-critical)",
                        extra={"issue_id": str(issue_id), "error": str(e)}
                    )
        else:  # after photos
            issue.after_photos = (issue.after_photos or []) + uploaded_urls

        # ──── PERSIST CHANGES ───────────────────────────────────────────────
        batch_commit(db, "upload_photos")
        db.refresh(issue)

        # ──── LOGGING & RESPONSE ────────────────────────────────────────────
        logger.info(
            "Photos uploaded successfully",
            extra={
                "issue_id": str(issue_id),
                "photo_type": photo_type,
                "photo_count": len(uploaded_urls),
                "uploader_id": str(current_user.id)
            }
        )

        result = IssueResponse.model_validate(issue)
        result.comment_count = _get_comment_count(db, issue.id)
        return result

    except CivicException:
        # Custom exceptions (ValidationError, AuthorizationError, etc.)
        # Already have proper status codes and details
        raise
    except Exception as e:
        logger.error(
            "Unexpected error uploading photos",
            extra={
                "issue_id": str(issue_id),
                "photo_type": photo_type,
                "uploader_id": str(current_user.id),
                "error": str(e)
            },
            exc_info=True
        )
        raise ProcessingError(
            "photo_upload",
            "Failed to upload photos. Please try again."
        )


@router.delete("/{issue_id}/photos", response_model=IssueResponse)
def delete_photo(
    issue_id: uuid.UUID,
    url: str = Query(..., description="Exact URL of the photo to remove"),
    photo_type: str = Query("before", pattern="^(before|after)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a specific photo from an issue.

    Pass the exact photo URL (as returned in ``before_photos`` / ``after_photos``)
    to remove it from the list.

    **Params**:
        - ``url``: Exact URL of the photo to remove (query param).
        - ``photo_type``: ``"before"`` or ``"after"`` (query param).

    Returns:
        Updated ``IssueResponse`` with the photo removed.

    Raises:
        403: User doesn't have permission to remove this photo type.
        404: Issue not found, or photo URL not found in the list.
    """
    issue = db.query(Issue).filter(Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")

    if photo_type == "before" and str(issue.reporter_id) != str(current_user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the reporter can remove before photos")
    if photo_type == "after" and current_user.role != "admin" and str(issue.assigned_worker_id) != str(current_user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the assigned worker or admin can remove after photos")

    try:
        if photo_type == "before":
            photos = [p for p in (issue.before_photos or []) if p != url]
            if len(photos) == len(issue.before_photos or []):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo URL not found in before_photos")
            issue.before_photos = photos
        else:
            photos = [p for p in (issue.after_photos or []) if p != url]
            if len(photos) == len(issue.after_photos or []):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo URL not found in after_photos")
            issue.after_photos = photos

        db.commit()
        db.refresh(issue)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting photo from issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Photo deletion failed. Please try again.",
        )

    logger.info(f"Photo removed from {photo_type}_photos of issue {issue_id}")
    result = IssueResponse.model_validate(issue)
    result.comment_count = _get_comment_count(db, issue.id)
    return result


@router.get("/nearby", response_model=List[IssueResponse])
def get_nearby_issues(
    lat: float = Query(..., description="Latitude of the center point"),
    lng: float = Query(..., description="Longitude of the center point"),
    radius_km: float = Query(2.0, le=20.0, description="Search radius in kilometers (max 20)"),
    issue_type: Optional[str] = Query(None, description="Filter by issue type"),
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """Get issues within a radius of given coordinates.

    Returns all non-closed issues within ``radius_km`` of the provided lat/lng.
    Uses Haversine formula for distance calculation.

    **Params**:
        - ``lat``, ``lng``: Center coordinates.
        - ``radius_km``: Search radius in km (default 2.0, max 20.0).
        - ``issue_type``: Optional filter (e.g. ``"pothole"``).

    Returns:
        List of ``IssueResponse`` objects within the radius.

    Raises:
        401: Not authenticated.
    """
    import math

    try:
        # Pre-filter with a bounding box in SQL to avoid full table scan in Python
        lat_delta = radius_km / 111.0
        lng_delta = radius_km / (111.0 * math.cos(math.radians(lat)))

        candidates = (
            db.query(Issue)
            .filter(
                Issue.status != "closed",
                Issue.is_deleted == False,
                Issue.latitude.between(lat - lat_delta, lat + lat_delta),
                Issue.longitude.between(lng - lng_delta, lng + lng_delta),
            )
            .all()
        )
        R = 6371  # km

        nearby = []
        for issue in candidates:
            phi1, phi2 = math.radians(lat), math.radians(issue.latitude)
            dphi = math.radians(issue.latitude - lat)
            dlam = math.radians(issue.longitude - lng)
            a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
            dist = R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            if dist <= radius_km:
                if issue_type is None or issue.issue_type == issue_type:
                    nearby.append(issue)
    except Exception as e:
        logger.error(f"Error fetching nearby issues at ({lat}, {lng}): {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch nearby issues.",
        )

    logger.info(f"Found {len(nearby)} issues within {radius_km}km of ({lat}, {lng})")
    response_items = [IssueResponse.model_validate(i) for i in nearby]
    _populate_comment_counts(db, response_items)
    return response_items


@router.get("", response_model=IssueListResponse)
def list_issues(
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    size: int = Query(20, ge=1, le=100, description="Items per page (max 100)"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    issue_type: Optional[str] = Query(None, description="Filter by issue type"),
    ward: Optional[str] = Query(None, description="Filter by ward"),
    priority: Optional[str] = Query(None, description="Filter by priority (critical, high, medium, low)"),
    sort: Optional[str] = Query(None, description="Sort order: newest (default), oldest, most_upvoted"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List issues with optional filters and pagination.

    Role-based visibility:
    - **Citizens**: see only their own reported issues.
    - **Workers**: see only their assigned issues.
    - **Admins**: see all issues.

    **Query params**: ``status``, ``issue_type``, ``ward``, ``priority``, ``sort``, ``page``, ``size``.

    Returns:
        ``IssueListResponse`` with items, total count, page, and size.

    Raises:
        401: Not authenticated.
    """
    try:
        query = db.query(Issue).filter(Issue.is_deleted == False)

        if current_user.role == "citizen":
            query = query.filter(Issue.reporter_id == current_user.id)
        elif current_user.role == "worker":
            query = query.filter(Issue.assigned_worker_id == current_user.id)

        if status_filter:
            query = query.filter(Issue.status == status_filter)
        if issue_type:
            query = query.filter(Issue.issue_type == issue_type)
        if ward:
            query = query.filter(Issue.ward == ward)
        if priority and priority in ("critical", "high", "medium", "low"):
            query = query.filter(Issue.priority == priority)

        total = query.count()
        if sort == "oldest":
            order = Issue.created_at.asc()
        elif sort == "most_upvoted":
            order = Issue.upvote_count.desc()
        else:
            order = Issue.created_at.desc()
        items = query.order_by(order, Issue.id.desc()).offset((page - 1) * size).limit(size).all()
    except Exception as e:
        logger.error(f"Error listing issues for user {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch issues.",
        )

    # Batch-check which issues the current user has upvoted
    user_voted_ids = set()
    if items:
        voted_rows = db.query(IssueVote.issue_id).filter(
            IssueVote.issue_id.in_([i.id for i in items]),
            IssueVote.user_id == current_user.id,
        ).all()
        user_voted_ids = {row[0] for row in voted_rows}

    # Batch fetch comment counts for all issues
    comment_counts = {}
    if items:
        from app.models.issue_comment import IssueComment
        comment_rows = db.query(IssueComment.issue_id, func.count(IssueComment.id)).filter(
            IssueComment.issue_id.in_([i.id for i in items])
        ).group_by(IssueComment.issue_id).all()
        comment_counts = {row[0]: row[1] for row in comment_rows}

    response_items = []
    for i in items:
        resp = IssueResponse.model_validate(i)
        resp.user_upvoted = i.id in user_voted_ids
        resp.comment_count = comment_counts.get(i.id, 0)
        response_items.append(resp)

    return IssueListResponse(
        items=response_items,
        total=total,
        page=page,
        size=size,
    )


@router.get("/following", response_model=IssueListResponse)
def list_following_issues(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return issues the current user has upvoted (following)."""
    try:
        voted_issue_ids = db.query(IssueVote.issue_id).filter(
            IssueVote.user_id == current_user.id
        ).subquery()

        query = db.query(Issue).filter(
            Issue.id.in_(voted_issue_ids),
            Issue.is_deleted == False,
            Issue.reporter_id != current_user.id,  # own issues are in "My Issues" tab
        )
        total = query.count()
        items = query.order_by(Issue.created_at.desc(), Issue.id.desc()).offset((page - 1) * size).limit(size).all()
    except Exception as e:
        logger.error(f"Error listing following issues for user {current_user.id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to fetch following issues.")

    from app.models.issue_comment import IssueComment
    comment_counts = {}
    if items:
        comment_rows = db.query(IssueComment.issue_id, func.count(IssueComment.id)).filter(
            IssueComment.issue_id.in_([i.id for i in items])
        ).group_by(IssueComment.issue_id).all()
        comment_counts = {row[0]: row[1] for row in comment_rows}

    response_items = []
    for i in items:
        resp = IssueResponse.model_validate(i)
        resp.user_upvoted = True  # all are upvoted by definition
        resp.comment_count = comment_counts.get(i.id, 0)
        response_items.append(resp)

    return IssueListResponse(items=response_items, total=total, page=page, size=size)


@router.get("/ward-health", response_model=dict)
def ward_health_score(
    ward: str = Query(..., description="Ward name/number to calculate health score for"),
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """Compute a 0-100 health score for a ward.

    The score is a weighted combination of:
    - **50%** resolution rate (resolved / total issues).
    - **30%** resolution speed (0h = perfect, 72h+ = zero).
    - **20%** citizen satisfaction (average rating / 5).

    Returns:
        ``{"ward", "score", "total_issues", "open_issues", "resolved_issues",
        "avg_resolution_hours", "avg_citizen_rating"}``

    Raises:
        401: Not authenticated.
    """
    try:
        base = db.query(Issue).filter(Issue.ward == ward, Issue.is_deleted == False)
        total = base.count()
        if not total:
            return {"ward": ward, "score": 100, "total_issues": 0, "open_issues": 0}

        open_count = base.filter(Issue.status.in_(["open", "assigned", "in_progress"])).count()
        resolved_count = base.filter(Issue.status.in_(["resolved", "closed"])).count()

        # Resolution rate factor (0-1)
        resolution_rate = resolved_count / total

        # Speed factor computed in SQL
        from sqlalchemy import extract
        avg_resolution_secs = (
            base.filter(Issue.status.in_(["resolved", "closed"]), Issue.resolved_at.isnot(None))
            .with_entities(
                func.avg(extract('epoch', Issue.resolved_at) - extract('epoch', Issue.created_at))
            )
            .scalar()
        )
        avg_hours_val = float(avg_resolution_secs) / 3600 if avg_resolution_secs else None
        speed_factor = max(0.0, 1.0 - (avg_hours_val / 72.0)) if avg_hours_val is not None else 0.5

        # Rating factor in SQL
        avg_rating_val = (
            base.filter(Issue.status.in_(["resolved", "closed"]), Issue.citizen_rating.isnot(None))
            .with_entities(func.avg(Issue.citizen_rating))
            .scalar()
        )
        rating_factor = (float(avg_rating_val) / 5.0) if avg_rating_val else 0.5

        score = round((resolution_rate * 0.5 + speed_factor * 0.3 + rating_factor * 0.2) * 100, 1)
    except Exception as e:
        logger.error(f"Error calculating ward health for '{ward}': {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to calculate ward health score.",
        )

    return {
        "ward": ward,
        "score": score,
        "total_issues": total,
        "open_issues": open_count,
        "resolved_issues": resolved_count,
        "avg_resolution_hours": round(avg_hours_val, 1) if avg_hours_val else None,
        "avg_citizen_rating": round(float(avg_rating_val), 2) if avg_rating_val else None,
    }


@router.post("/{issue_id}/reopen", response_model=IssueResponse)
def reopen_issue(
    issue_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reopen a resolved issue if the citizen is unsatisfied.

    Clears the resolution, sets status back to ``open``, removes the worker assignment,
    and attempts to auto-assign a new worker.

    **Roles**: citizen (own issues only), admin (any issue). Workers cannot reopen.

    Returns:
        Updated ``IssueResponse``.

    Raises:
        400: Issue is not in ``resolved`` status.
        403: Workers cannot reopen; citizens can only reopen their own issues.
        404: Issue not found.
    """
    issue = db.query(Issue).filter(Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")

    if current_user.role == "worker":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workers cannot reopen issues")
    if current_user.role == "citizen" and str(issue.reporter_id) != str(current_user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    if issue.status != "resolved":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only resolved issues can be reopened")

    try:
        issue.status = "open"
        issue.assigned_worker_id = None
        issue.resolved_at = None
        # Preserve resolution_notes as audit trail (prefix with REOPENED marker)
        if issue.resolution_notes:
            issue.resolution_notes = f"[REOPENED] {issue.resolution_notes}"
        # Reset AI resolution assessment (no longer valid after reopen)
        issue.ai_is_resolved = None
        issue.ai_resolution_quality = None
        issue.ai_resolution_notes = None
        db.commit()
        db.refresh(issue)

        # Notify all admins that the issue was reopened and needs attention
        try:
            admins = db.query(User).filter(User.role == "admin", User.is_active == True).all()
            for admin_user in admins:
                notify_localized(
                    db=db, user=admin_user, key="reopened",
                    notification_type="system", issue_id=str(issue.id),
                    issue_id_short=str(issue.id)[:8],
                    issue_type=issue.issue_type, ward=issue.ward or "unknown",
                )
        except Exception:
            pass

        # Try to auto-reassign
        from app.services.geo_service import auto_assign
        if auto_assign(issue, db):
            db.commit()
            db.refresh(issue)
            if issue.assigned_worker:
                notify_localized(db=db, user=issue.assigned_worker, key="assignment",
                                 notification_type="assignment", issue_id=str(issue.id),
                                 issue_type=issue.issue_type, ward=issue.ward or "your area")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reopening issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reopen issue. Please try again.",
        )

    logger.info(f"Issue {issue_id} reopened by user {current_user.id}")
    result = IssueResponse.model_validate(issue)
    result.comment_count = _get_comment_count(db, issue.id)
    return result


@router.get("/{issue_id}/timeline", response_model=list[dict])
def get_issue_timeline(
    issue_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a chronological timeline of status events for an issue.

    Derived from stored timestamps — no separate event table.
    Events include: reported, assigned, in_progress, escalated, blocked, resolved.

    Returns:
        List of ``{"event": str, "label": str, "at": datetime}`` sorted chronologically.

    Raises:
        404: Issue not found.
    """
    issue = db.query(Issue).filter(Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")

    events = [{"event": "reported", "label": "Issue reported", "at": issue.created_at}]

    if issue.assigned_worker_id:
        events.append({"event": "assigned", "label": "Assigned to a worker", "at": issue.updated_at})

    if issue.status in ("in_progress", "resolved", "closed"):
        events.append({"event": "in_progress", "label": "Worker started working", "at": issue.updated_at})

    if issue.is_escalated and issue.escalated_at:
        events.append({"event": "escalated", "label": "Issue escalated", "at": issue.escalated_at})

    if issue.is_blocked:
        events.append({"event": "blocked", "label": f"Blocked: {issue.blocked_reason}", "at": issue.updated_at})

    if issue.status in ("resolved", "closed") and issue.resolved_at:
        events.append({"event": "resolved", "label": "Issue resolved", "at": issue.resolved_at})

    events.sort(key=lambda e: e["at"] or issue.created_at)
    return events


@router.get("/{issue_id}", response_model=IssueResponse)
def get_issue(
    issue_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a single issue by its UUID.

    Citizens can view any issue. Workers can only view their assigned issues. Admins can view any issue.

    Returns:
        ``IssueResponse`` with all fields including AI analysis and photos.

    Raises:
        404: Issue not found.
    """
    issue = db.query(Issue).filter(Issue.id == issue_id, Issue.is_deleted == False).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")

    # Workers can only view their own assigned issues; citizens can view any issue
    if current_user.role == "worker" and str(issue.assigned_worker_id) != str(current_user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    user_voted = db.query(IssueVote).filter(
        IssueVote.issue_id == issue_id,
        IssueVote.user_id == current_user.id,
    ).first() is not None

    result = IssueResponse.model_validate(issue)
    result.user_upvoted = user_voted
    result.comment_count = _get_comment_count(db, issue.id)
    return result


@router.patch("/{issue_id}", response_model=IssueResponse)
def update_issue(
    issue_id: uuid.UUID,
    body: IssueUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update issue fields. Role-based restrictions apply.

    - **Citizens**: can set ``citizen_rating`` on their own resolved issues.
    - **Workers/Admins**: can update ``status`` and ``resolution_notes``.
    - **Admins only**: can set ``assigned_worker_id`` (triggers reassignment to ``assigned`` status).

    Returns:
        Updated ``IssueResponse``.

    Raises:
        403: Citizen trying to update another user's issue.
        404: Issue not found.
    """
    issue = db.query(Issue).filter(Issue.id == issue_id, Issue.is_deleted == False).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")

    try:
        # Citizens can only rate their own resolved issues, or close their own resolved issues
        if current_user.role == "citizen":
            if str(issue.reporter_id) != str(current_user.id):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
            if body.citizen_rating is not None:
                was_rated = issue.citizen_rating is not None
                issue.citizen_rating = body.citizen_rating
                # Reward citizen for rating (only first time)
                if not was_rated:
                    try:
                        from app.services.rewards_service import award_event
                        award_event(db, current_user.id, "rate_issue", reference_id=issue.id)
                        # If 5-star, reward the worker too
                        if body.citizen_rating == 5 and issue.assigned_worker_id:
                            award_event(db, issue.assigned_worker_id, "five_star_rating",
                                        reference_id=issue.id, note="Citizen gave 5 stars")
                    except Exception:
                        pass
            if body.status == "closed" and issue.status == "resolved":
                issue.status = "closed"

        # A worker may only touch the issue actually assigned to them. There was
        # no such check here — unlike POST /workers/tasks/{id}/resolve, which has
        # always filtered on assigned_worker_id — so any worker could PATCH any
        # other worker's issue to "resolved", closing someone else's task and
        # firing a resolution notification at the citizen.
        if current_user.role == "worker" and issue.assigned_worker_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This issue is not assigned to you",
            )

        # Sub-admins were matched by none of the branches below, so a ward_admin
        # got 200 OK with nothing written — a silent no-op. They are routed with
        # the super-admin now, gated on their geographic scope.
        is_scoped_admin = current_user.role in ADMIN_ROLES
        if is_scoped_admin and current_user.role != "admin":
            in_scope = apply_admin_scope(
                db.query(Issue.id).filter(Issue.id == issue.id), current_user, Issue
            ).first()
            if not in_scope:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="This issue is outside your administrative jurisdiction",
                )

        if current_user.role == "worker" or is_scoped_admin:
            if body.status is not None:
                # FIX: HIGH PRIORITY BUG #3 - Validate status transitions
                _validate_status_transition(issue.status, body.status, current_user.role)
                
                # Urgent/high priority issues require an after-photo before resolving
                if (
                    body.status == "resolved"
                    and current_user.role == "worker"
                    and issue.priority in ("urgent", "high")
                    and not issue.after_photos
                ):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"After-photo is required to resolve a {issue.priority}-priority issue. "
                               "Upload via POST /issues/{id}/photos?photo_type=after first.",
                    )
                issue.status = body.status
            if body.resolution_notes is not None:
                issue.resolution_notes = body.resolution_notes

        if is_scoped_admin:
            if body.assigned_worker_id is not None:
                issue.assigned_worker_id = body.assigned_worker_id
                issue.status = "assigned"

        db.commit()
        db.refresh(issue)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update issue. Please try again.",
        )

    logger.info(f"Issue {issue_id} updated by user {current_user.id}")

    # ── Post-update notifications ─────────────────────────────────────────────
    try:
        # Admin manually assigned a worker → notify the worker
        if is_scoped_admin and body.assigned_worker_id is not None and issue.assigned_worker:
            notify_localized(
                db=db, user=issue.assigned_worker, key="assignment",
                notification_type="assignment", issue_id=str(issue.id),
                issue_type=issue.issue_type, ward=issue.ward or "your area",
            )

        # Admin/worker status change → notify reporter
        if body.status is not None and (current_user.role == "worker" or is_scoped_admin) and issue.reporter:
            if body.status == "resolved":
                notify_localized(
                    db=db, user=issue.reporter, key="resolution",
                    notification_type="resolution", issue_id=str(issue.id),
                    issue_type=issue.issue_type, ward=issue.ward or "your area",
                )
            elif body.status == "in_progress":
                notify_localized(
                    db=db, user=issue.reporter, key="in_progress",
                    notification_type="status_update", issue_id=str(issue.id),
                    issue_type=issue.issue_type, ward=issue.ward or "your area",
                )

        # Citizen closes the issue → notify assigned worker
        if (
            current_user.role == "citizen"
            and body.status == "closed"
            and issue.assigned_worker
        ):
            notify_localized(
                db=db, user=issue.assigned_worker, key="closed",
                notification_type="status_update", issue_id=str(issue.id),
                issue_id_short=str(issue.id)[:8],
                issue_type=issue.issue_type, ward=issue.ward or "your area",
            )

        # Citizen rates the resolution → notify worker with their score
        if (
            current_user.role == "citizen"
            and body.citizen_rating is not None
            and issue.assigned_worker
        ):
            notify_localized(
                db=db, user=issue.assigned_worker, key="rated",
                notification_type="status_update", issue_id=str(issue.id),
                issue_type=issue.issue_type, ward=issue.ward or "your area",
                rating=body.citizen_rating,
            )
    except Exception:
        pass  # notifications are best-effort

    result = IssueResponse.model_validate(issue)
    result.comment_count = _get_comment_count(db, issue.id)
    return result


# ── Issue comments ────────────────────────────────────────────────────────────


def _check_comment_access(issue: Issue, current_user: User, db: Session) -> None:
    """Enforce who may read and write comments on an issue.

    - Citizens: only issues they reported.
    - Workers: only issues assigned to them.
    - Admins: only issues inside their geographic scope (super-admins: all).

    This was an empty ``pass`` while the docstrings of both callers stated these
    exact rules and documented a 403 that could never fire. The restriction is
    not novel — ``list_issues`` already limits a citizen to
    ``Issue.reporter_id == current_user.id``, so a citizen could not see another
    citizen's issue in a listing but could read and post on its comment thread
    by issue ID.

    Raises:
        HTTPException 403: The user has no access to this issue.
    """
    if current_user.role == "citizen":
        allowed = issue.reporter_id == current_user.id
    elif current_user.role == "worker":
        allowed = issue.assigned_worker_id == current_user.id
    elif current_user.role in ADMIN_ROLES:
        allowed = bool(
            apply_admin_scope(
                db.query(Issue.id).filter(Issue.id == issue.id), current_user, Issue
            ).first()
        )
    else:
        allowed = False

    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this issue",
        )


@router.post("/{issue_id}/comments", response_model=IssueCommentResponse, status_code=status.HTTP_201_CREATED)
def add_comment(
    issue_id: uuid.UUID,
    body: IssueCommentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Post a comment/update on an issue.

    Access control:
    - **Citizens**: own issues only.
    - **Workers**: assigned issues only.
    - **Admins**: any issue.

    Returns:
        ``IssueCommentResponse`` with comment details and author info.

    Raises:
        403: User doesn't have access to this issue.
        404: Issue not found.
        400: Comment nesting exceeds maximum depth (prevents DoS via deep replies).
    """
    issue = db.query(Issue).filter(Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")

    _check_comment_access(issue, current_user, db)

    # MEDIUM PRIORITY BUG FIX #3: Comment nesting DoS protection
    MAX_COMMENT_DEPTH = 5
    
    # Validate parent_id if provided
    if body.parent_id:
        parent = db.query(IssueComment).filter(
            IssueComment.id == body.parent_id,
            IssueComment.issue_id == issue_id,
        ).first()
        if not parent:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent comment not found")
        
        # Check nesting depth to prevent DoS
        depth = 1
        current_parent = parent
        while current_parent.parent_id:
            depth += 1
            current_parent = db.query(IssueComment).filter(
                IssueComment.id == current_parent.parent_id
            ).first()
            if not current_parent:
                break
            if depth >= MAX_COMMENT_DEPTH:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Comment nesting exceeds maximum depth of {MAX_COMMENT_DEPTH} levels"
                )

    # Only workers and admins may post internal notes; silently downgrade for citizens
    is_internal = body.is_internal and current_user.role in (
        "worker", "admin", "district_admin", "taluka_admin", "ward_admin"
    )

    try:
        comment = IssueComment(
            issue_id=issue_id,
            author_id=current_user.id,
            body=body.body.strip(),
            parent_id=body.parent_id,
            is_internal=is_internal,
        )
        db.add(comment)
        db.commit()
        db.refresh(comment)
    except Exception as e:
        logger.error(f"Error adding comment to issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to add comment. Please try again.",
        )

    logger.info(f"Comment added to issue {issue_id} by user {current_user.id}")

    # Notify reporter when a worker or admin posts a public comment on their issue
    try:
        if (
            not is_internal
            and current_user.role in ("worker", "admin", "district_admin", "taluka_admin", "ward_admin")
            and issue.reporter
            and str(issue.reporter_id) != str(current_user.id)
        ):
            notify_localized(
                db=db, user=issue.reporter, key="comment",
                notification_type="status_update", issue_id=str(issue.id),
                issue_type=issue.issue_type, ward=issue.ward or "your area",
            )
        # Notify the parent comment's author when someone replies (skip self-replies)
        if body.parent_id and parent:
            parent_author = db.query(User).filter(User.id == parent.author_id).first()
            if parent_author and str(parent_author.id) != str(current_user.id):
                notify_localized(
                    db=db, user=parent_author, key="reply",
                    notification_type="status_update", issue_id=str(issue.id),
                    issue_type=issue.issue_type, ward=issue.ward or "your area",
                )
    except Exception:
        pass  # notifications are best-effort

    resp = IssueCommentResponse.model_validate(comment)
    resp.replies = []
    return resp


@router.get("/{issue_id}/comments", response_model=List[IssueCommentResponse])
def list_comments(
    issue_id: uuid.UUID,
    limit: int = Query(200, ge=1, le=500, description="Maximum comments to return"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List comments on an issue in chronological order.

    Access control:
    - **Citizens**: own issues only, and only public comments.
    - **Workers**: assigned issues only.
    - **Admins**: issues within their jurisdiction.

    Bounded by ``limit``. This was an unbounded ``.all()`` that then built the
    full reply tree in memory, so one heavily-discussed issue could return an
    arbitrarily large response.

    Returns:
        List of ``IssueCommentResponse`` objects.

    Raises:
        403: User doesn't have access to this issue.
        404: Issue not found.
    """
    issue = db.query(Issue).filter(Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")

    _check_comment_access(issue, current_user, db)

    # Citizens only see public comments; workers and admins see everything
    can_see_internal = current_user.role in (
        "worker", "admin", "district_admin", "taluka_admin", "ward_admin"
    )
    q = db.query(IssueComment).filter(IssueComment.issue_id == issue_id)
    if not can_see_internal:
        # Citizens: hide internal notes AND comments authored by workers
        q = (
            q.join(User, User.id == IssueComment.author_id)
             .filter(IssueComment.is_internal == False)  # noqa: E712
             .filter(User.role.notin_(["worker"]))
        )
    all_comments = q.order_by(IssueComment.created_at).limit(limit).all()

    # Build nested structure: top-level comments with replies
    comment_map = {c.id: IssueCommentResponse.model_validate(c) for c in all_comments}
    for resp in comment_map.values():
        resp.replies = []

    top_level = []
    for c in all_comments:
        resp = comment_map[c.id]
        if c.parent_id and c.parent_id in comment_map:
            comment_map[c.parent_id].replies.append(resp)
        elif c.parent_id is None:
            top_level.append(resp)

    return top_level


@router.delete("/{issue_id}/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_comment(
    issue_id: uuid.UUID,
    comment_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a comment from an issue.

    Permission rules:
    - A user can always delete their **own** comment.
    - Admins can delete **any** comment on any issue.
    - Citizens and workers cannot delete others' comments.

    Raises:
        403: Not the author and not an admin.
        404: Issue or comment not found.
    """
    issue = db.query(Issue).filter(Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")

    comment = db.query(IssueComment).filter(
        IssueComment.id == comment_id,
        IssueComment.issue_id == issue_id,
    ).first()
    if not comment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")

    if current_user.role != "admin" and str(comment.author_id) != str(current_user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own comments",
        )

    try:
        db.delete(comment)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting comment {comment_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete comment.",
        )

    logger.info(f"Comment {comment_id} deleted by user {current_user.id}")


# NOTE: Upvote POST/DELETE endpoints are defined in features.py with
# rewards integration and auto-escalation logic.


# ───────────────────────────────────────────────────────────────────────────────
# SLA & Auto-Escalation Endpoints (Admin Only)
# ───────────────────────────────────────────────────────────────────────────────

@router.get("/sla/dashboard", response_model=dict)
def sla_dashboard(
    current_user: User = Depends(require_role("admin", "district_admin", "taluka_admin", "ward_admin")),
    db: Session = Depends(get_db),
):
    """SLA Dashboard: overview of issues violating or approaching SLA Thresholds.
    
    Provides metrics on:
    - Escalated issues count and details
    - Issues approaching SLA breach
    - SLA compliance rate by priority
    
    **Roles**: admin only.
    
    Returns:
        {
            "escalated_count": int,
            "escalated_issues": [...],
            "at_risk_count": int,
            "at_risk_issues": [...],
            "compliance_by_priority": {...},
            "updated_at": ISO timestamp
        }
    """
    try:
        now = datetime.now(timezone.utc)
        
        # SLA thresholds (hours)
        SLA_THRESHOLDS = {
            "urgent": 24,
            "high": 48,
            "medium": 72,
            "low": 168,
        }
        
        # Fetch all open/assigned/in_progress issues
        open_issues = (
            db.query(Issue)
            .filter(
                Issue.status.in_(["open", "assigned", "in_progress"]),
                Issue.is_deleted == False
            )
            .all()
        )
        
        escalated_issues = []
        at_risk_issues = []
        
        for issue in open_issues:
            sla_hours = SLA_THRESHOLDS.get(issue.priority, 72)
            cutoff = issue.created_at + timedelta(hours=sla_hours)
            time_remaining = cutoff - now
            hours_remaining = time_remaining.total_seconds() / 3600
            
            issue_data = {
                "id": str(issue.id),
                "id_short": str(issue.id)[:8],
                "issue_type": issue.issue_type,
                "priority": issue.priority,
                "status": issue.status,
                "ward": issue.ward,
                "created_at": issue.created_at.isoformat() if issue.created_at else None,
                "sla_hours": sla_hours,
                "hours_remaining": round(hours_remaining, 1),
                "is_escalated": issue.is_escalated,
                "escalated_at": issue.escalated_at.isoformat() if issue.escalated_at else None,
            }
            
            if issue.is_escalated:
                escalated_issues.append(issue_data)
            elif hours_remaining < 2:  # Less than 2 hours until breach
                at_risk_issues.append(issue_data)
        
        # Compliance by priority
        compliance_by_priority = {}
        for priority in ["urgent", "high", "medium", "low"]:
            priority_issues = [i for i in open_issues if i.priority == priority]
            if priority_issues:
                sla_hours = SLA_THRESHOLDS[priority]
                compliant = sum(
                    1 for i in priority_issues
                    if (now - i.created_at).total_seconds() / 3600 <= sla_hours
                )
                compliance_by_priority[priority] = {
                    "total": len(priority_issues),
                    "compliant": compliant,
                    "violated": len(priority_issues) - compliant,
                    "compliance_rate": round((compliant / len(priority_issues) * 100), 1),
                }
        
        return {
            "escalated_count": len(escalated_issues),
            "escalated_issues": sorted(escalated_issues, key=lambda x: x["created_at"]),
            "at_risk_count": len(at_risk_issues),
            "at_risk_issues": sorted(at_risk_issues, key=lambda x: x["hours_remaining"]),
            "compliance_by_priority": compliance_by_priority,
            "updated_at": now.isoformat(),
        }
    except Exception as e:
        logger.error(f"Error computing SLA dashboard: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch SLA dashboard.",
        )


@router.get("/sla/{issue_id}", response_model=dict)
def issue_sla_status(
    issue_id: uuid.UUID,
    current_user: User = Depends(require_role("admin", "district_admin", "taluka_admin", "ward_admin")),
    db: Session = Depends(get_db),
):
    """Get detailed SLA status for a specific issue.
    
    Shows:
    - SLA threshold based on priority
    - Time spent vs. SLA
    - Escalation history
    
    **Roles**: admin only.
    
    Returns:
        {
            "issue_id": str,
            "priority": str,
            "status": str,
            "created_at": ISO timestamp,
            "sla_hours": int,
            "time_elapsed_hours": float,
            "time_remaining_hours": float,
            "sla_breach": bool,
            "is_escalated": bool,
            "escalated_at": ISO timestamp | null,
            "escalation_reason": str
        }
    """
    try:
        issue = db.query(Issue).filter(Issue.id == issue_id).first()
        if not issue:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")
        
        SLA_THRESHOLDS = {
            "urgent": 24,
            "high": 48,
            "medium": 72,
            "low": 168,
        }
        
        now = datetime.now(timezone.utc)
        sla_hours = SLA_THRESHOLDS.get(issue.priority, 72)
        sla_cutoff = issue.created_at + timedelta(hours=sla_hours)
        time_elapsed = (now - issue.created_at).total_seconds() / 3600
        time_remaining = (sla_cutoff - now).total_seconds() / 3600
        sla_breach = now > sla_cutoff
        
        escalation_reason = ""
        if issue.is_escalated and sla_breach:
            hours_overdue = (now - sla_cutoff).total_seconds() / 3600
            escalation_reason = f"SLA violated by {round(hours_overdue, 1)}h ({sla_hours}h threshold)"
        
        return {
            "issue_id": str(issue.id),
            "priority": issue.priority,
            "status": issue.status,
            "created_at": issue.created_at.isoformat() if issue.created_at else None,
            "sla_hours": sla_hours,
            "time_elapsed_hours": round(time_elapsed, 1),
            "time_remaining_hours": round(time_remaining, 1),
            "sla_breach": sla_breach,
            "is_escalated": issue.is_escalated,
            "escalated_at": issue.escalated_at.isoformat() if issue.escalated_at else None,
            "escalation_reason": escalation_reason,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching SLA status for issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch SLA status.",
        )


# ── Multilingual Voice Transcription ─────────────────────────────────────────

@router.post("/transcribe", response_model=dict)
async def transcribe_audio(
    audio: UploadFile = File(..., description="Audio file (.m4a, .wav, .mp3, .ogg)"),
    language: str = Query("hi", description="Language code (e.g. hi, mr, en, gu, ta)"),
    current_user: User = Depends(get_current_user),
):
    """Transcribe an audio file to text using OpenAI Whisper (or fallback).

    Supports multilingual voice reporting — citizens can speak in their
    local language and the audio is transcribed to text for issue creation.

    **Roles**: any authenticated user.

    Args:
        audio: Uploaded audio file.
        language: BCP-47 language hint for the transcription model.

    Returns:
        ``{ "text": "transcribed content", "language": "hi", "duration_seconds": 12.5 }``
    """
    import os
    ALLOWED_TYPES = {"audio/m4a", "audio/mp4", "audio/mpeg", "audio/wav", "audio/ogg", "audio/x-m4a", "audio/aac"}
    ALLOWED_EXTENSIONS = {".m4a", ".mp4", ".mp3", ".wav", ".ogg", ".aac"}
    MAX_SIZE = 25 * 1024 * 1024  # 25 MB

    try:
        # Validate file type. Both the extension and the declared content type
        # must be acceptable — this was `ext not in ALLOWED and ctype not in
        # ALLOWED`, i.e. rejected only when *both* were wrong, so `payload.exe`
        # with `Content-Type: audio/mpeg` sailed through.
        ext = os.path.splitext(audio.filename or "")[1].lower()
        ctype = (audio.content_type or "").split(";")[0].strip()
        if ext not in ALLOWED_EXTENSIONS or ctype not in ALLOWED_TYPES:
            raise HTTPException(status_code=400, detail="Unsupported audio format. Use .m4a, .mp3, .wav, or .ogg.")

        # Read audio data
        audio_data = await audio.read()
        if len(audio_data) > MAX_SIZE:
            raise HTTPException(status_code=400, detail="Audio file too large. Maximum 25 MB.")
        if len(audio_data) < 1000:
            raise HTTPException(status_code=400, detail="Audio file too small or empty.")

        logger.info(f"Transcription request: user={current_user.id}, lang={language}, size={len(audio_data)}")

        # A feature that is switched off is a 501, not a 200 with an empty
        # string. Returning `{"text": ""}` is indistinguishable from a
        # successful transcription of silence, so the client had no way to tell
        # the difference and would show the user a blank result.
        if not settings.OPENAI_API_KEY:
            logger.warning("Transcription requested but OPENAI_API_KEY is not set")
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="Audio transcription is not configured on this server.",
            )

        try:
            import httpx

            # Bytes straight into the multipart body. This used to write the
            # upload to a NamedTemporaryFile and read it back — synchronous disk
            # I/O for up to 25 MB inside an `async def`, blocking the event loop
            # twice over, purely to hand httpx a file object it did not need.
            async with httpx.AsyncClient(timeout=settings.AI_REQUEST_TIMEOUT_SECONDS * 2) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                    files={"file": (audio.filename or "audio.m4a", audio_data, ctype or "audio/m4a")},
                    data={"model": "whisper-1", "language": language},
                )

            if resp.status_code == 200:
                text = resp.json().get("text", "").strip()
                logger.info(f"Whisper transcription success: {len(text)} chars")
                return {"text": text, "language": language, "model": "whisper-1"}

            logger.warning(f"Whisper API error {resp.status_code}: {resp.text[:200]}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Transcription provider returned an error. Please try again.",
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Whisper transcription failed: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Transcription is temporarily unavailable. Please try again.",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Transcription error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Transcription failed.")


