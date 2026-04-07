"""
Worker Routes
=============
Endpoints for worker task management, location tracking, and leaderboard.

Frontend Integration Notes:
- All endpoints require ``role="worker"`` (some also allow admin).
- Task lifecycle: assigned → accept → in_progress → resolve (with after-photo upload).
- Workers can also reject (reassigns) or block (notifies admins) tasks.
- Location should be updated periodically by the worker app for geo-routing.
"""

import uuid
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import require_role
from app.core.logger import get_logger
from app.database import get_db
from app.models.issue import Issue
from app.models.user import User
from app.schemas.issue import IssueResponse
from app.schemas.worker import TaskAcceptReject, WorkerStats, WorkerStatusUpdate, LocationUpdate
from app.services.ai_service import verify_resolution
from app.services.notification_service import notify_bookmarkers, notify_localized, send_sms_status_update
from app.services.storage import upload_image
from app.services.utils import get_users_by_role

logger = get_logger("workers")

router = APIRouter(prefix="/workers", tags=["Workers"])

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@router.put("/status", response_model=dict)
def update_worker_status(
    body: WorkerStatusUpdate,
    current_user: User = Depends(require_role("worker", "admin")),
    db: Session = Depends(get_db),
):
    """Toggle worker online/offline availability.

    Workers must be online to receive new task assignments via auto-routing.

    **Roles**: worker, admin.

    Returns:
        ``{"is_online": true/false}``

    Raises:
        401: Not authenticated.
        403: User role is not worker or admin.
    """
    try:
        current_user.is_online = body.is_online
        db.commit()
    except Exception as e:
        logger.error(f"Error updating status for worker {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update status. Please try again.",
        )

    logger.info(f"Worker {current_user.id} status changed to {'online' if body.is_online else 'offline'}")
    return {"is_online": current_user.is_online}


# NOTE: PUT /availability endpoint is defined in features.py with typed Pydantic model.


@router.get("/leaderboard", response_model=list[dict])
def worker_leaderboard(
    current_user: User = Depends(require_role("worker")),
    db: Session = Depends(get_db),
):
    """Get the worker leaderboard with the current worker's rank highlighted.

    Score formula: ``tasks_resolved * avg_rating`` (default rating = 3.0 if unrated).
    The response includes ``"is_me": true`` for the current worker's entry.

    **Roles**: worker.

    Returns:
        List of ``{"worker_id", "name", "ward", "tasks_resolved", "avg_rating", "score", "rank", "is_me"}``

    Raises:
        401: Not authenticated.
        403: Not a worker.
    """
    try:
        from sqlalchemy import func as _func, case as _case
        from app.models.issue import Issue as _Issue

        # Single aggregated query instead of N+1 per-worker queries
        stats_q = (
            db.query(
                _Issue.assigned_worker_id,
                _func.count(_Issue.id).label("resolved"),
                _func.avg(_Issue.citizen_rating).label("avg_rating"),
            )
            .filter(_Issue.status.in_(["resolved", "closed"]))
            .group_by(_Issue.assigned_worker_id)
            .subquery()
        )

        workers = get_users_by_role("worker", db)
        worker_ids = {w.id for w in workers}

        # Fetch stats in one go
        from sqlalchemy.orm import Session as _S
        stat_rows = (
            db.query(stats_q.c.assigned_worker_id, stats_q.c.resolved, stats_q.c.avg_rating)
            .filter(stats_q.c.assigned_worker_id.in_(worker_ids))
            .all()
        )
        stats_map = {row[0]: {"resolved": row[1], "avg_rating": row[2]} for row in stat_rows}

        board = []
        for w in workers:
            s = stats_map.get(w.id, {"resolved": 0, "avg_rating": None})
            resolved = s["resolved"] or 0
            avg_r = round(float(s["avg_rating"]), 2) if s["avg_rating"] else None
            total_points = round(resolved * (avg_r or 3.0), 1)

            # Simple level tiers based on tasks completed
            if resolved >= 100:
                level, level_name = 5, "Expert"
            elif resolved >= 50:
                level, level_name = 4, "Advanced"
            elif resolved >= 20:
                level, level_name = 3, "Skilled"
            elif resolved >= 5:
                level, level_name = 2, "Rookie"
            else:
                level, level_name = 1, "Newcomer"

            board.append({
                "worker_id": str(w.id),
                "name": w.name or w.phone,
                "ward": w.ward,
                "tasks_completed": resolved,
                "avg_rating": avg_r,
                "total_points": total_points,
                "level": level,
                "level_name": level_name,
                "is_me": w.id == current_user.id,
            })

        board.sort(key=lambda x: x["total_points"], reverse=True)
        for i, entry in enumerate(board):
            entry["rank"] = i + 1
    except Exception as e:
        logger.error(f"Error fetching leaderboard: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch leaderboard.",
        )

    return board


@router.get("/tasks/history", response_model=list[IssueResponse])
def get_task_history(
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    size: int = Query(20, ge=1, le=100, description="Items per page"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status: assigned | in_progress | resolved | closed"),
    current_user: User = Depends(require_role("worker")),
    db: Session = Depends(get_db),
):
    """Get all tasks ever assigned to the current worker (paginated).

    Returns tasks in all statuses, sorted by most recently updated first.

    **Roles**: worker.

    Returns:
        List of ``IssueResponse`` objects.

    Raises:
        401: Not authenticated.
        403: Not a worker.
    """
    valid_statuses = {"assigned", "in_progress", "resolved", "closed"}
    try:
        q = db.query(Issue).filter(Issue.assigned_worker_id == current_user.id)
        if status_filter and status_filter in valid_statuses:
            q = q.filter(Issue.status == status_filter)
        else:
            # Default: history = completed tasks only (resolved or closed)
            q = q.filter(Issue.status.in_(["resolved", "closed"]))
        tasks = (
            q
            .order_by(Issue.resolved_at.desc().nullslast(), Issue.updated_at.desc())
            .offset((page - 1) * size)
            .limit(size)
            .all()
        )
    except Exception as e:
        logger.error(f"Error fetching task history for worker {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch task history.",
        )

    return [IssueResponse.model_validate(t) for t in tasks]


@router.get("/tasks", response_model=list[IssueResponse])
def get_my_tasks(
    current_user: User = Depends(require_role("worker")),
    db: Session = Depends(get_db),
):
    """Get all active tasks assigned to the current worker.

    Tasks are sorted by severity (high → medium → low), then by creation date.
    Only includes tasks with status ``assigned`` or ``in_progress``.

    **Roles**: worker.

    Returns:
        List of ``IssueResponse`` objects sorted by priority.

    Raises:
        401: Not authenticated.
        403: Not a worker.
    """
    try:
        tasks = (
            db.query(Issue)
            .filter(
                Issue.assigned_worker_id == current_user.id,
                Issue.status.in_(["assigned", "in_progress"]),
            )
            .all()
        )
        tasks.sort(key=lambda i: (SEVERITY_ORDER.get(i.severity, 1), i.created_at))
    except Exception as e:
        logger.error(f"Error fetching tasks for worker {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch tasks.",
        )

    return [IssueResponse.model_validate(t) for t in tasks]


@router.post("/tasks/{issue_id}/accept", response_model=IssueResponse)
def accept_task(
    issue_id: uuid.UUID,
    current_user: User = Depends(require_role("worker")),
    db: Session = Depends(get_db),
):
    """Accept an assigned task and set its status to ``in_progress``.

    Notifies the citizen that a worker has started working on their issue.

    **Roles**: worker.

    Returns:
        Updated ``IssueResponse`` with status ``"in_progress"``.

    Raises:
        400: Task is not in ``assigned`` or ``in_progress`` status.
        404: Task not found or not assigned to this worker.
    """
    issue = db.query(Issue).filter(
        Issue.id == issue_id,
        Issue.assigned_worker_id == current_user.id,
    ).first()
    if not issue:
        logger.warning(f"Worker {current_user.id} tried to accept non-existent/unassigned task {issue_id}")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found or not assigned to you")

    if issue.status not in ("assigned", "in_progress"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Task cannot be accepted in its current state")

    try:
        issue.status = "in_progress"
        db.commit()
        db.refresh(issue)

        # Notify the citizen that work has started
        if issue.reporter:
            notify_localized(
                db=db,
                user=issue.reporter,
                key="in_progress",
                notification_type="status_update",
                issue_id=str(issue.id),
                action_type="open_issue",
                issue_type=issue.issue_type,
                ward=issue.ward or "your area",
            )
            # SMS notification (#6)
            send_sms_status_update(
                phone=issue.reporter.phone,
                lang=getattr(issue.reporter, "language", "en"),
                sms_key="in_progress",
                issue_type=issue.issue_type,
                issue_id_short=str(issue.id)[:8],
            )
        # Notify bookmarkers (#8)
        notify_bookmarkers(db, issue, "in_progress")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error accepting task {issue_id} by worker {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to accept task. Please try again.",
        )

    logger.info(f"Worker {current_user.id} accepted task {issue_id}")
    return IssueResponse.model_validate(issue)


@router.post("/tasks/{issue_id}/reject", response_model=IssueResponse)
def reject_task(
    issue_id: uuid.UUID,
    body: TaskAcceptReject,
    current_user: User = Depends(require_role("worker")),
    db: Session = Depends(get_db),
):
    """Reject an assigned task.

    Clears the worker assignment, sets status to ``open``, and attempts to
    re-assign the nearest available worker (excluding the rejecting worker).
    Notifies the citizen about the reassignment.

    **Roles**: worker.

    Returns:
        Updated ``IssueResponse`` (may have a new worker assigned).

    Raises:
        400: Action is not ``"reject"``.
        404: Task not found or not assigned to this worker.
    """
    if body.action != "reject":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="action must be 'reject'")

    issue = db.query(Issue).filter(
        Issue.id == issue_id,
        Issue.assigned_worker_id == current_user.id,
    ).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found or not assigned to you")

    REJECTION_ESCALATION_THRESHOLD = 3  # auto-escalate after this many rejections

    try:
        rejected_worker_id = current_user.id

        # Track ALL prior rejecters so geo_service excludes all of them
        prior_rejected_ids = list(issue.rejected_by_ids or [])
        if str(rejected_worker_id) not in prior_rejected_ids:
            prior_rejected_ids.append(str(rejected_worker_id))
        issue.rejected_by_ids = prior_rejected_ids

        issue.assigned_worker_id = None
        issue.status = "open"
        issue.reassignment_count = (issue.reassignment_count or 0) + 1

        # Append rejection note without overwriting prior notes
        rejection_note = f"[Rejected by worker {str(rejected_worker_id)[:8]}]"
        if body.reason:
            rejection_note += f": {body.reason}"
        if issue.resolution_notes:
            issue.resolution_notes = issue.resolution_notes + "\n" + rejection_note
        else:
            issue.resolution_notes = rejection_note
        db.commit()

        # Auto-escalate if rejection threshold reached
        if issue.reassignment_count >= REJECTION_ESCALATION_THRESHOLD and not issue.is_escalated:
            issue.is_escalated = True
            issue.escalated_at = datetime.utcnow()
            db.commit()
            admins = get_users_by_role("admin", db)
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
            logger.warning(f"Issue {issue_id} auto-escalated after {issue.reassignment_count} rejections")

        # Try to re-assign — exclude ALL workers who have previously rejected this issue
        from app.services.geo_service import find_nearest_worker
        import uuid as _uuid
        excluded_ids = [_uuid.UUID(wid) for wid in prior_rejected_ids if _is_valid_uuid(wid)]
        next_worker = find_nearest_worker(
            issue.latitude, issue.longitude, issue.ward, db,
            exclude_worker_ids=excluded_ids,
        )
        if next_worker:
            issue.assigned_worker_id = next_worker.id
            issue.status = "assigned"
            db.commit()
            # Notify the new worker
            notify_localized(
                db=db,
                user=next_worker,
                key="assignment",
                notification_type="assignment",
                issue_id=str(issue.id),
                issue_type=issue.issue_type,
                ward=issue.ward or "your area",
            )
            # Notify the citizen about reassignment
            if issue.reporter:
                notify_localized(
                    db=db,
                    user=issue.reporter,
                    key="rejection",
                    notification_type="status_update",
                    issue_id=str(issue.id),
                    issue_type=issue.issue_type,
                    ward=issue.ward or "your area",
                )
            logger.info(f"Rejected task {issue_id} reassigned to worker {next_worker.id}")
        else:
            # No worker found — notify admins so the issue doesn't silently go unattended
            logger.warning(f"No alternate worker found after rejection of task {issue_id}")
            admins = get_users_by_role("admin", db)
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
            # Notify citizen so they know their issue is awaiting manual assignment
            if issue.reporter:
                notify_localized(
                    db=db,
                    user=issue.reporter,
                    key="rejection",
                    notification_type="status_update",
                    issue_id=str(issue.id),
                    issue_type=issue.issue_type,
                    ward=issue.ward or "your area",
                )

        db.refresh(issue)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error rejecting task {issue_id} by worker {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reject task. Please try again.",
        )

    logger.info(f"Worker {current_user.id} rejected task {issue_id}: {body.reason}")
    return IssueResponse.model_validate(issue)


def _is_valid_uuid(val: str) -> bool:
    try:
        import uuid as _u
        _u.UUID(val)
        return True
    except (ValueError, AttributeError):
        return False


@router.post("/tasks/{issue_id}/resolve", response_model=IssueResponse)
async def resolve_task(
    issue_id: uuid.UUID,
    after_photo: UploadFile = File(...),
    resolution_notes: Optional[str] = Form(None),
    current_user: User = Depends(require_role("worker")),
    db: Session = Depends(get_db),
):
    """Mark a task as resolved by uploading an after-photo.

    The after-photo is verified by AI to assess resolution quality.
    If AI detects a poor resolution, all admins are notified for review.
    The citizen is notified that their issue has been resolved.

    **Request format**: ``multipart/form-data`` (not JSON).
    - ``after_photo``: Image file (required).
    - ``resolution_notes``: Text notes (optional form field).

    **Roles**: worker.

    Returns:
        Updated ``IssueResponse`` with resolution details and AI assessment.

    Raises:
        400: Task is not in ``in_progress`` status.
        404: Task not found or not assigned to this worker.
    """
    issue = db.query(Issue).filter(
        Issue.id == issue_id,
        Issue.assigned_worker_id == current_user.id,
    ).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found or not assigned to you")

    if issue.status != "in_progress":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Task must be in_progress to resolve")

    ALLOWED_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    MAX_PHOTO_BYTES = 10 * 1024 * 1024  # 10 MB

    try:
        file_bytes = await after_photo.read()

        if len(file_bytes) > MAX_PHOTO_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Photo is too large. Maximum allowed size is 10 MB.",
            )

        mime_type = (after_photo.content_type or "image/jpeg").split(";")[0].strip()
        if mime_type not in ALLOWED_PHOTO_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid file type '{mime_type}'. Only JPEG, PNG, WebP, and GIF images are allowed.",
            )
        filename = f"after_{uuid.uuid4().hex[:8]}"
        url = upload_image(file_bytes, filename, user_id=str(current_user.id), issue_id=str(issue_id))
        if url:
            issue.after_photos = (issue.after_photos or []) + [url]

        # AI verification of resolution
        ai_result = await verify_resolution(file_bytes, mime_type)
        issue.ai_is_resolved = ai_result.get("is_resolved")
        issue.ai_resolution_quality = ai_result.get("resolution_quality")
        issue.ai_resolution_notes = ai_result.get("notes")

        issue.status = "resolved"
        issue.resolved_at = datetime.utcnow()
        if resolution_notes:
            issue.resolution_notes = resolution_notes

        db.commit()
        db.refresh(issue)

        # If AI says resolution quality is poor, notify all admins for review
        if ai_result.get("resolution_quality") == "poor" and ai_result.get("is_resolved") is False:
            admins = get_users_by_role("admin", db)
            for admin_user in admins:
                notify_localized(
                    db=db,
                    user=admin_user,
                    key="poor_resolution",
                    notification_type="system",
                    issue_id=str(issue.id),
                    issue_id_short=str(issue.id)[:8],
                    issue_type=issue.issue_type,
                    ward=issue.ward or "unknown",
                )
            logger.warning(f"AI flagged poor resolution for task {issue_id}")

        # Notify the citizen their issue is resolved
        if issue.reporter:
            notify_localized(
                db=db,
                user=issue.reporter,
                key="resolution",
                notification_type="resolution",
                issue_id=str(issue.id),
                action_type="rate_issue",
                issue_type=issue.issue_type,
                ward=issue.ward or "your area",
            )
            # SMS notification (#6)
            send_sms_status_update(
                phone=issue.reporter.phone,
                lang=getattr(issue.reporter, "language", "en"),
                sms_key="resolved",
                issue_type=issue.issue_type,
                issue_id_short=str(issue.id)[:8],
            )
        # Notify bookmarkers (#8)
        notify_bookmarkers(db, issue, "resolved")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resolving task {issue_id} by worker {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resolve task. Please try again.",
        )

    logger.info(f"Worker {current_user.id} resolved task {issue_id} (AI quality: {ai_result.get('resolution_quality')})")

    # Rewards: worker earns points for resolving
    try:
        from app.services.rewards_service import award_event
        award_event(db, current_user.id, "resolve_issue", reference_id=issue.id)
        # Fast resolve bonus: resolved within 24h of assignment
        if issue.resolved_at and issue.updated_at:
            hours_taken = (issue.resolved_at - issue.updated_at).total_seconds() / 3600
            if hours_taken <= 24:
                award_event(db, current_user.id, "fast_resolve", reference_id=issue.id,
                            note=f"Resolved in {round(hours_taken, 1)}h (< 24h)")
        # Citizen earns points when their issue is resolved
        if issue.reporter_id:
            award_event(db, issue.reporter_id, "issue_resolved", reference_id=issue.id)
    except Exception:
        pass

    return IssueResponse.model_validate(issue)


@router.get("/stats", response_model=WorkerStats)
def get_worker_stats(
    current_user: User = Depends(require_role("worker")),
    db: Session = Depends(get_db),
):
    """Get today's task statistics for the current worker.

    **Roles**: worker.

    Returns:
        ``WorkerStats`` with tasks_assigned_today, tasks_completed_today,
        tasks_pending, and avg_rating.

    Raises:
        401: Not authenticated.
        403: Not a worker.
    """
    try:
        from datetime import timezone, timedelta
        # IST today window for "completed today"
        IST = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(IST)
        today_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start = today_start_ist.astimezone(timezone.utc)

        # Total assigned (all time, any status)
        assigned_today = db.query(func.count(Issue.id)).filter(
            Issue.assigned_worker_id == current_user.id,
        ).scalar() or 0

        # Total completed (resolved or closed — closed is the final state after resolution)
        completed_today = db.query(func.count(Issue.id)).filter(
            Issue.assigned_worker_id == current_user.id,
            Issue.status.in_(["resolved", "closed"]),
        ).scalar() or 0

        pending = db.query(func.count(Issue.id)).filter(
            Issue.assigned_worker_id == current_user.id,
            Issue.status.in_(["assigned", "in_progress"]),
        ).scalar() or 0

        avg_rating_row = db.query(func.avg(Issue.citizen_rating)).filter(
            Issue.assigned_worker_id == current_user.id,
            Issue.citizen_rating.isnot(None),
        ).scalar()
    except Exception as e:
        logger.error(f"Error fetching stats for worker {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch worker stats.",
        )

    return WorkerStats(
        tasks_assigned_today=assigned_today,
        tasks_completed_today=completed_today,
        tasks_pending=pending,
        avg_rating=round(float(avg_rating_row), 2) if avg_rating_row else None,
    )


@router.put("/location", response_model=dict)
def update_location(
    body: LocationUpdate,
    current_user: User = Depends(require_role("worker")),
    db: Session = Depends(get_db),
):
    """Update the worker's live GPS location.

    Should be called periodically by the worker app (e.g. every 30-60 seconds)
    to enable geo-based task routing.

    **Roles**: worker.

    Returns:
        ``{"latitude", "longitude", "updated_at"}``

    Raises:
        401: Not authenticated.
        403: Not a worker.
    """
    try:
        current_user.latitude = body.latitude
        current_user.longitude = body.longitude
        current_user.location_updated_at = datetime.utcnow()
        db.commit()
    except Exception as e:
        logger.error(f"Error updating location for worker {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update location.",
        )

    return {"latitude": body.latitude, "longitude": body.longitude, "updated_at": current_user.location_updated_at}


@router.post("/tasks/{issue_id}/block", response_model=IssueResponse)
def block_task(
    issue_id: uuid.UUID,
    reason: str = Query(..., min_length=3, description="Why the task is blocked"),
    current_user: User = Depends(require_role("worker")),
    db: Session = Depends(get_db),
):
    """Flag a task as blocked and notify all admins.

    Use when the task cannot proceed — e.g. needs equipment, external department
    involvement, or the location is inaccessible. The task stays in ``in_progress``
    with ``is_blocked=true``.

    **Roles**: worker.

    Returns:
        Updated ``IssueResponse`` with blocked status.

    Raises:
        400: Task is not in ``assigned`` or ``in_progress`` status.
        404: Task not found or not assigned to this worker.
    """
    issue = db.query(Issue).filter(
        Issue.id == issue_id,
        Issue.assigned_worker_id == current_user.id,
    ).first()
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found or not assigned to you")

    if issue.status not in ("assigned", "in_progress"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Task must be assigned or in_progress to block")

    try:
        issue.is_blocked = True
        issue.blocked_reason = reason
        issue.status = "in_progress"
        db.commit()
        db.refresh(issue)

        # Notify all active admins
        admins = get_users_by_role("admin", db)
        issue_id_short = str(issue.id)[:8]
        for admin in admins:
            notify_localized(
                db=db,
                user=admin,
                key="blocked",
                notification_type="system",
                issue_id=str(issue.id),
                issue_id_short=issue_id_short,
                reason=reason,
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error blocking task {issue_id} by worker {current_user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to block task. Please try again.",
        )

    logger.info(f"Worker {current_user.id} blocked task {issue_id}: {reason}")
    return IssueResponse.model_validate(issue)