"""
Admin Routes
============
Endpoints for the admin dashboard, worker/citizen/sub-admin management,
announcements, bulk operations, analytics, and escalation.

Admin Role Hierarchy (ascending privilege):
    ward_admin < taluka_admin < district_admin < admin (super)

Access control:
- All admin roles can access most endpoints.
- Issue/user queries are automatically scoped to the admin's geographic area.
- Sub-admin creation is limited by hierarchy (e.g. ward_admin cannot create admins).
- Only super-admin can access cross-district data or change roles.

Frontend Integration Notes:
- Dashboard stats are live (no caching).
- CSV export returns a downloadable file with ``Content-Disposition`` header.
- Heatmap data returns lat/lng/weight for map visualization libraries.
"""

import math
import re
import secrets
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func

from app.core.time import now_utc
from app.services.geofence_utils import (
    check_geofences_intersect,
    haversine_distance,
    point_in_geofence,
    validate_geofence_radius,
    validate_geofence_area,
)
from sqlalchemy.orm import Session

from app.core.deps import apply_admin_scope, require_any_admin, require_role, user_scope_filter
from app.core.logger import get_logger
from app.core.security import hash_password
from app.services.email_service import send_worker_invitation
from app.database import get_db
from app.models.announcement import Announcement
from app.models.geofence import Geofence
from app.models.geofence_alert import GeofenceAlert
from app.models.issue import Issue
from app.models.issue_flag import IssueFlag
from app.models.location import Taluka, Ward
from app.models.user import User
from app.core.config import settings
from app.schemas.admin import (
    AssignWorker, CreateWorker, CreateGeofenceRequest, DashboardStats,
    GeofenceListResponse, GeofenceResponse, UpdateWorker, UpdateSubAdmin,
    UpdateGeofenceRequest, WorkerInvitationResult
)
from app.schemas.auth import UserResponse
# Admins get the variants carrying the reporter's phone number. Every other
# router uses the plain IssueResponse, which has no phone field to emit — see
# the docstring on IssueReporterInfo.
from app.schemas.issue import IssueAdminListResponse, IssueAdminResponse
from app.services.utils import (
    apply_not_deleted_filter,
    apply_active_filter,
)
from app.services.auth_service import revoke_all_user_tokens
from app.services.admin_override import (
    log_override_access,
)
from app.core.exceptions import ResourceNotFoundError, ValidationError, ProcessingError

logger = get_logger("admin")

router = APIRouter(prefix="/admin", tags=["Admin"])


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_model=DashboardStats)
def get_dashboard(
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Get live dashboard statistics scoped to the admin's geographic area.

    **Roles**: any admin.
    """
    try:
        today_start = datetime.combine(date.today(), datetime.min.time())

        base = db.query(Issue)
        base = apply_admin_scope(base, current_user, Issue)

        total_open = base.filter(Issue.status == "open").count()
        total_in_progress = base.filter(Issue.status.in_(["assigned", "in_progress"])).count()
        total_resolved_today = base.filter(
            Issue.status == "resolved", Issue.resolved_at >= today_start
        ).count()
        total_issues = base.count()

        total_workers_online = (
            db.query(func.count(User.id))
            .filter(User.role == "worker", User.is_online == True)
            .scalar() or 0
        )

        # Compute average resolution time in SQL (avoids loading all resolved issues into Python)
        from sqlalchemy import extract
        avg_resolution_seconds = (
            apply_admin_scope(db.query(
                func.avg(
                    extract('epoch', Issue.resolved_at) - extract('epoch', Issue.created_at)
                )
            ), current_user, Issue)
            .filter(Issue.status == "resolved", Issue.resolved_at.isnot(None))
            .scalar()
        )
        avg_hours = round(float(avg_resolution_seconds) / 3600, 1) if avg_resolution_seconds else None
    except Exception as e:
        logger.error(f"Error fetching dashboard stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load dashboard data.")

    return DashboardStats(
        total_open=total_open,
        total_in_progress=total_in_progress,
        total_resolved_today=total_resolved_today,
        total_issues=total_issues,
        avg_resolution_hours=avg_hours,
        total_workers_online=total_workers_online,
    )


# ── Issues ────────────────────────────────────────────────────────────────────

@router.get("/issues", response_model=IssueAdminListResponse)
def list_all_issues(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    status_filter: Optional[str] = Query(None, alias="status"),
    issue_type: Optional[str] = Query(None),
    ward: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    priority: Optional[str] = Query(None, description="Filter by priority: urgent/high/medium/low"),
    department: Optional[str] = Query(None, description="Filter by department"),
    ai_flag: Optional[str] = Query(
        None,
        description="Drill-down for the insights screen: low_confidence | poor_resolution",
    ),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """List all issues in the admin's scope with optional filters (paginated).

    ``ai_flag`` exists so the AI-insights screen can page its drill-down lists
    server-side. It previously fetched up to 2,000 issues and filtered them in
    the browser, which both truncated the result and made the counts a sample.

    **Roles**: any admin.
    """
    # Validated before the try: the handler's `except Exception` turns anything
    # raised inside into a 500, so a deliberate 422 raised in there would reach
    # the client as "Failed to fetch issues".
    if ai_flag is not None and ai_flag not in ("low_confidence", "poor_resolution"):
        # Not silently ignored — an unrecognised value returning the unfiltered
        # list would show every issue under a "low confidence" tab.
        raise HTTPException(
            status_code=422,
            detail="ai_flag must be 'low_confidence' or 'poor_resolution'",
        )

    try:
        query = apply_admin_scope(db.query(Issue), current_user, Issue)
        query = apply_not_deleted_filter(query)

        if status_filter:
            query = query.filter(Issue.status == status_filter)
        if issue_type:
            query = query.filter(Issue.issue_type == issue_type)
        if ward:
            query = query.filter(Issue.ward == ward)
        if severity:
            query = query.filter(Issue.severity == severity)
        if priority:
            query = query.filter(Issue.priority == priority)
        if department:
            query = query.filter(Issue.department == department)
        if ai_flag == "low_confidence":
            query = query.filter(
                Issue.ai_confidence.isnot(None), Issue.ai_confidence < AI_LOW_CONFIDENCE
            )
        elif ai_flag == "poor_resolution":
            query = query.filter(Issue.ai_resolution_quality == "poor")

        total = query.count()
        items = query.order_by(Issue.created_at.desc(), Issue.id.desc()).offset((page - 1) * size).limit(size).all()
    except Exception as e:
        logger.error(f"Error listing issues: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch issues.")

    return IssueAdminListResponse(
        items=[IssueAdminResponse.model_validate(i) for i in items],
        total=total, page=page, size=size,
    )


@router.post("/issues/{issue_id}/reassign", response_model=IssueAdminResponse)
def reassign_worker(
    issue_id: uuid.UUID,
    body: AssignWorker,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Reassign an issue to a different worker.
    
    Validates:
    - Worker role must be 'worker'
    - Worker must be active, online, and accepting tasks
    - Worker must be in admin's jurisdiction
    - Worker workload < 100 active tasks
    - Worker must NOT be in the issue's rejected_by_ids list
    
    **Roles**: any admin.
    """
    # STEP 1: Verify issue exists in admin's jurisdiction
    issue = apply_admin_scope(db.query(Issue), current_user, Issue).filter(Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found or not in your jurisdiction")
    
    # STEP 2: Fetch worker and verify ROLE
    worker = db.query(User).filter(User.id == body.worker_id).first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    if worker.role != "worker":
        raise HTTPException(status_code=400, detail="User is not a worker")
    
    # STEP 3: Verify worker is ACTIVE
    if not worker.is_active:
        raise HTTPException(status_code=400, detail="Worker is inactive or suspended")
    
    # STEP 4: Verify worker is ONLINE
    if not worker.is_online:
        raise HTTPException(status_code=400, detail="Worker is offline (not connected)")
    
    # STEP 5: Verify worker is AVAILABLE (accepting new tasks)
    if not worker.is_available:
        raise HTTPException(status_code=400, detail="Worker is not accepting new assignments")
    
    # STEP 6: Verify worker is in JURISDICTION (admin's scope)
    # FIX: Properly validate scope filters - was using zip(scope, scope) which always evaluates to true
    scope = user_scope_filter(current_user)
    if scope:
        # Create a test query with worker and scope filters to validate jurisdiction
        test_query = db.query(User).filter(User.id == worker.id, *scope).first()
        if not test_query:
            raise HTTPException(status_code=403, detail="Worker is not in your administrative jurisdiction")
    
    # STEP 7: Check worker's WORKLOAD (active task count)
    active_task_count = db.query(Issue).filter(
        Issue.assigned_worker_id == worker.id,
        Issue.status.in_(["assigned", "in_progress"])
    ).count()
    if active_task_count >= 100:
        raise HTTPException(status_code=400, detail=f"Worker has {active_task_count} active tasks (limit: 100)")
    
    # STEP 8: Verify worker NOT in REJECTED_BY_IDS (didn't reject this issue)
    rejected_list = issue.rejected_by_ids or []
    if str(worker.id) in [str(rid) for rid in rejected_list]:
        raise HTTPException(status_code=400, detail="This worker previously rejected this issue")
    
    # STEP 9: Perform reassignment
    try:
        old_worker_id = issue.assigned_worker_id
        issue.assigned_worker_id = worker.id
        issue.status = "assigned"
        db.commit()
        db.refresh(issue)
        
        # Notify new worker
        from app.services.notification_service import notify_localized
        notify_localized(
            db=db, user=worker, key="assignment", notification_type="assignment",
            issue_id=str(issue.id), issue_type=issue.issue_type, ward=issue.ward or "your area"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reassigning issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to reassign issue.")
    
    logger.info(
        f"Admin {current_user.id} reassigned issue {issue_id} from {old_worker_id} to {worker.id}",
        extra={"old_worker_id": str(old_worker_id), "new_worker_id": str(worker.id), "active_tasks": active_task_count}
    )
    return IssueAdminResponse.model_validate(issue)


@router.post("/issues/{issue_id}/unblock", response_model=IssueAdminResponse)
def unblock_task(
    issue_id: uuid.UUID,
    admin_notes: str = Query(..., min_length=1, max_length=500, description="Why task is being unblocked"),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Admin unblocks a task and clears the blocked flag.
    
    Updates:
    - is_blocked → false
    - unblocked_at → now
    - unblocked_by_id → current admin
    - admin_unblock_note → provided reason
    - block_resolved_by → 'unblock'
    
    Notifies assigned worker that block is cleared.
    
    **Roles**: any admin.
    """
    issue = apply_admin_scope(db.query(Issue), current_user, Issue).filter(Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found or not in your jurisdiction")
    
    if not issue.is_blocked:
        raise HTTPException(status_code=400, detail="Task is not blocked")
    
    try:
        issue.is_blocked = False
        issue.unblocked_at = now_utc()
        issue.unblocked_by_id = current_user.id
        issue.admin_unblock_note = admin_notes
        issue.block_resolved_by = "unblock"
        db.commit()
        db.refresh(issue)
        
        # Notify assigned worker that block is cleared
        if issue.assigned_worker:
            from app.services.notification_service import notify_localized
            notify_localized(
                db=db,
                user=issue.assigned_worker,
                key="unblocked",
                notification_type="status_update",
                issue_id=str(issue.id),
                issue_type=issue.issue_type,
                admin_note=admin_notes[:100],  # Truncate for notification
                ward=issue.ward or "your area"
            )
        
        logger.info(
            f"Admin {current_user.id} unblocked issue {issue_id}. "
            f"Reason: {admin_notes}",
            extra={"issue_id": str(issue_id), "admin_id": str(current_user.id)}
        )
    except Exception as e:
        logger.error(f"Error unblocking issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to unblock task")
    
    return IssueAdminResponse.model_validate(issue)


@router.get("/blocked-tasks", response_model=IssueAdminListResponse)
def list_blocked_tasks(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sort: str = Query("blocked_duration", description="blocked_duration | blocked_at | status"),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """List all blocked tasks in admin's jurisdiction.
    
    Filters: is_blocked=true
    Sorting: by block duration (oldest first) or other fields
    
    **Roles**: any admin.
    """
    from datetime import datetime, timezone
    
    base_query = apply_admin_scope(db.query(Issue), current_user, Issue)
    base_query = base_query.filter(Issue.is_blocked == True)
    
    # Apply sorting
    if sort == "blocked_at":
        base_query = base_query.order_by(Issue.blocked_at.asc())  # oldest first
    elif sort == "status":
        base_query = base_query.order_by(Issue.status, Issue.blocked_at.asc())
    else:  # blocked_duration (default)
        base_query = base_query.order_by(Issue.blocked_at.asc())  # oldest = longest duration
    
    total = base_query.count()
    issues = base_query.limit(limit).offset(offset).all()
    
    # Enrich with block duration
    responses = []
    for issue in issues:
        resp = IssueAdminResponse.model_validate(issue)
        if issue.blocked_at:
            blocked_duration = (datetime.now(timezone.utc) - issue.blocked_at).total_seconds() / 3600
            resp.blocked_duration_hours = round(blocked_duration, 1)
        responses.append(resp)
    
    return IssueAdminListResponse(items=responses, total=total, limit=limit, offset=offset)


@router.post("/issues/{issue_id}/respond-to-block", response_model=IssueAdminResponse)
def respond_to_block(
    issue_id: uuid.UUID,
    message: str = Query(..., min_length=1, max_length=500, description="Message to worker about block"),
    resources_provided: str = Query("", description="Comma-separated list of resources provided"),
    can_proceed: bool = Query(False, description="Whether worker can now proceed"),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Admin responds to a blocked task without clearing the block.
    
    Use this endpoint when:
    - Acknowledging the block
    - Informing worker of provided resources
    - Providing partial resolution
    - Asking for more information
    
    Does NOT clear is_blocked flag. Worker can still call unblock if resolved.
    
    **Roles**: any admin.
    """
    issue = apply_admin_scope(db.query(Issue), current_user, Issue).filter(Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found or not in your jurisdiction")
    
    if not issue.is_blocked:
        raise HTTPException(status_code=400, detail="Task is not blocked")
    
    try:
        # Create or append admin response in resolution_notes
        response_text = f"\n[Admin Response from {current_user.name or current_user.id}]: {message}"
        if resources_provided:
            response_text += f"\nResources provided: {resources_provided}"
        if can_proceed:
            response_text += "\n✓ You can proceed with the task."
        
        if issue.resolution_notes:
            issue.resolution_notes = issue.resolution_notes + response_text
        else:
            issue.resolution_notes = response_text
        
        db.commit()
        db.refresh(issue)
        
        # Notify assigned worker of response
        if issue.assigned_worker:
            from app.services.notification_service import notify_localized
            notify_localized(
                db=db,
                user=issue.assigned_worker,
                key="block_response",
                notification_type="status_update",
                issue_id=str(issue.id),
                message=message[:150],
                can_proceed=can_proceed,
                ward=issue.ward or "your area"
            )
        
        logger.info(
            f"Admin {current_user.id} responded to blocked issue {issue_id}. "
            f"Can proceed: {can_proceed}",
            extra={"issue_id": str(issue_id), "admin_id": str(current_user.id), "can_proceed": can_proceed}
        )
    except Exception as e:
        logger.error(f"Error responding to block on issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to respond to block")
    
    return IssueAdminResponse.model_validate(issue)


@router.post("/blocked-tasks/bulk-unblock", response_model=dict)
def bulk_unblock_tasks(
    issue_ids: list = Query(..., description="List of issue IDs to unblock"),
    admin_notes: str = Query(..., min_length=1, max_length=500, description="Reason for bulk unblock"),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Bulk unblock multiple tasks at once.
    
    Use for:
    - Disaster scenarios (e.g., "equipment now available for all")
    - Policy changes
    - Bulk resource allocation
    
    Validates each issue is:
    - In admin's jurisdiction
    - Currently blocked
    
    Returns count of successfully unblocked tasks.
    
    **Roles**: any admin.
    """
    if not issue_ids or len(issue_ids) == 0:
        raise HTTPException(status_code=400, detail="At least one issue ID required")
    
    if len(issue_ids) > 500:
        raise HTTPException(status_code=400, detail="Maximum 500 issues per bulk operation")
    
    unblocked_count = 0
    errors = []
    
    for issue_id_str in issue_ids:
        try:
            issue_id = uuid.UUID(issue_id_str)
            issue = apply_admin_scope(db.query(Issue), current_user, Issue).filter(
                Issue.id == issue_id
            ).first()
            
            if not issue:
                errors.append(f"{issue_id_str}: not found or not in jurisdiction")
                continue
            
            if not issue.is_blocked:
                errors.append(f"{issue_id_str}: not blocked")
                continue
            
            # Unblock the task
            issue.is_blocked = False
            issue.unblocked_at = now_utc()
            issue.unblocked_by_id = current_user.id
            issue.admin_unblock_note = admin_notes
            issue.block_resolved_by = "bulk_unblock"
            unblocked_count += 1
        except ValueError:
            errors.append(f"{issue_id_str}: invalid UUID format")
            continue
        except Exception as e:
            logger.error(f"Error unblocking {issue_id_str}: {e}")
            errors.append(f"{issue_id_str}: error during unblock")
            continue
    
    # Batch commit
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Bulk unblock commit failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to commit bulk unblock")
    
    logger.info(
        f"Admin {current_user.id} bulk unblocked {unblocked_count}/{len(issue_ids)} tasks. "
        f"Reason: {admin_notes}",
        extra={"admin_id": str(current_user.id), "count": unblocked_count, "total": len(issue_ids)}
    )
    
    return {
        "unblocked_count": unblocked_count,
        "total_requested": len(issue_ids),
        "errors": errors,
        "message": f"Successfully unblocked {unblocked_count} task(s)"
    }


@router.post("/issues/{issue_id}/assign", response_model=IssueAdminResponse)
def assign_worker(
    issue_id: uuid.UUID,
    body: AssignWorker,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Manually assign a worker to an issue.
    
    Validates:
    - Worker role must be 'worker'
    - Worker must be active, online, and accepting tasks
    - Worker must be in admin's jurisdiction
    - Worker workload < 100 active tasks
    
    **Roles**: any admin.
    """
    # STEP 1: Verify issue exists in admin's jurisdiction
    issue = apply_admin_scope(db.query(Issue), current_user, Issue).filter(Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found or not in your jurisdiction")
    
    # STEP 2-6: Fetch worker with full validation including jurisdiction checks
    # FIX HIGH PRIORITY BUG #1: Location hierarchy validation
    # Ensure worker's location is within admin's scope
    query = db.query(User).filter(User.id == body.worker_id, User.role == "worker")
    
    # Verify worker is within admin's jurisdiction using location hierarchy
    # Admin can only assign workers in their scope:
    # - super-admin: any worker
    # - district_admin: workers in their district
    # - taluka_admin: workers in their taluka
    # - ward_admin: workers in their ward
    if current_user.role != "admin":  # super-admin can assign anyone
        if current_user.ward_id:
            # Ward admin can only assign workers in their ward
            query = query.filter(User.ward_id == current_user.ward_id)
        elif current_user.taluka_id:
            # Taluka admin can only assign workers in their taluka's wards
            from app.models.location import Ward as WardModel
            taluka_wards = db.query(WardModel.id).filter(WardModel.taluka_id == current_user.taluka_id).subquery()
            query = query.filter(User.ward_id.in_(taluka_wards))
        elif current_user.district_id:
            # District admin can only assign workers in their district's talukas/wards
            from app.models.location import Taluka as TalukaModel, Ward as WardModel
            district_talukas = db.query(TalukaModel.id).filter(TalukaModel.district_id == current_user.district_id).subquery()
            taluka_wards = db.query(WardModel.id).filter(WardModel.taluka_id.in_(district_talukas)).subquery()
            query = query.filter(User.ward_id.in_(taluka_wards))
    
    # Apply jurisdiction scope filters (additional safety check)
    scope_filters = user_scope_filter(current_user)
    if scope_filters:
        query = query.filter(*scope_filters)
    
    worker = query.first()
    if not worker:
        from app.core.exceptions import AuthorizationError
        raise AuthorizationError(
            "assign",
            "worker",
            {"reason": "Worker not found or outside your jurisdiction", "worker_id": str(body.worker_id)}
        )
    
    # Verify worker is ACTIVE
    if not worker.is_active:
        raise HTTPException(status_code=400, detail="Worker is inactive or suspended")
    
    # Verify worker is ONLINE
    if not worker.is_online:
        raise HTTPException(status_code=400, detail="Worker is offline (not connected)")
    
    # Verify worker is AVAILABLE (accepting new tasks)
    if not worker.is_available:
        raise HTTPException(status_code=400, detail="Worker is not accepting new assignments")
    
    # STEP 7: Check worker's WORKLOAD (active task count)
    active_task_count = db.query(Issue).filter(
        Issue.assigned_worker_id == worker.id,
        Issue.status.in_(["assigned", "in_progress"])
    ).count()
    if active_task_count >= 100:
        raise HTTPException(status_code=400, detail=f"Worker has {active_task_count} active tasks (limit: 100)")
    
    # STEP 8: Perform assignment
    try:
        issue.assigned_worker_id = worker.id
        issue.status = "assigned"
        db.commit()
        db.refresh(issue)
        
        # Notify worker
        from app.services.notification_service import notify_localized
        notify_localized(
            db=db, user=worker, key="assignment", notification_type="assignment",
            issue_id=str(issue.id), issue_type=issue.issue_type, ward=issue.ward or "your area",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error assigning worker to issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to assign worker.")
    
    logger.info(
        f"Admin {current_user.id} assigned worker {worker.id} to issue {issue_id}",
        extra={"worker_id": str(worker.id), "active_tasks": active_task_count}
    )
    return IssueAdminResponse.model_validate(issue)


@router.post("/issues/{issue_id}/escalate", response_model=IssueAdminResponse)
def escalate_issue(
    issue_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Manually escalate an issue. **Roles**: any admin."""
    issue = apply_admin_scope(db.query(Issue), current_user, Issue).filter(Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    if issue.is_escalated:
        raise HTTPException(status_code=400, detail="Issue is already escalated")

    try:
        issue.is_escalated = True
        issue.escalated_at = now_utc()
        db.commit()
        db.refresh(issue)
    except Exception as e:
        logger.error(f"Error escalating issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to escalate issue.")

    logger.info(f"Issue {issue_id} escalated by admin {current_user.id}")
    return IssueAdminResponse.model_validate(issue)


# ── Bulk operations ───────────────────────────────────────────────────────────

class BulkIssueRequest(BaseModel):
    """Bulk operation request body."""
    issue_ids: List[uuid.UUID] = Field(..., min_length=1, max_length=100, description="List of issue UUIDs")
    action: str = Field(..., description="Action: close | escalate | assign")
    worker_id: Optional[uuid.UUID] = Field(None, description="Required for 'assign' action")
    priority: Optional[str] = Field(None, description="For 'set_priority' action: urgent/high/medium/low")


@router.post("/issues/bulk")
def bulk_issue_action(
    body: BulkIssueRequest,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Perform bulk actions on multiple issues.

    Actions:
    - ``close``        — set status to ``closed``
    - ``escalate``     — set ``is_escalated=true``
    - ``assign``       — assign all to a specific worker (requires ``worker_id``)
    - ``set_priority`` — update priority (requires ``priority``)

    **Roles**: any admin (scoped to their area).

    Returns:
        ``{"processed": int, "skipped": int}``
    """
    allowed = {"close", "escalate", "assign", "set_priority"}
    if body.action not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid action. Must be one of: {', '.join(allowed)}")

    if body.action == "assign" and not body.worker_id:
        raise HTTPException(status_code=400, detail="worker_id required for 'assign' action")
    if body.action == "set_priority" and not body.priority:
        raise HTTPException(status_code=400, detail="priority required for 'set_priority' action")

    worker = None
    if body.action == "assign":
        worker_q = db.query(User).filter(User.id == body.worker_id, User.role == "worker")
        worker_q = apply_active_filter(worker_q)
        worker = worker_q.first()
        if not worker:
            raise HTTPException(status_code=404, detail="Worker not found or inactive")

    try:
        issues = (
            apply_admin_scope(db.query(Issue), current_user, Issue)
            .filter(Issue.id.in_(body.issue_ids))
            .all()
        )

        processed = 0
        for issue in issues:
            if body.action == "close":
                issue.status = "closed"
                processed += 1
            elif body.action == "escalate" and not issue.is_escalated:
                issue.is_escalated = True
                issue.escalated_at = now_utc()
                processed += 1
            elif body.action == "assign" and worker:
                issue.assigned_worker_id = worker.id
                issue.status = "assigned"
                processed += 1
                # Notify worker of assignment
                try:
                    from app.services.notification_service import notify_localized
                    notify_localized(
                        db=db, user=worker, key="assignment",
                        notification_type="assignment", issue_id=str(issue.id),
                        issue_type=issue.issue_type, ward=issue.ward or "your area",
                    )
                except Exception:
                    pass  # don't break the loop if notification fails
            elif body.action == "set_priority":
                issue.priority = body.priority
                processed += 1

        db.commit()
        skipped = len(body.issue_ids) - processed
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Bulk action error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Bulk operation failed.")

    logger.info(f"Admin {current_user.id} bulk '{body.action}': {processed} processed, {skipped} skipped")
    return {"processed": processed, "skipped": skipped}


@router.post("/issues/auto-assign-open", response_model=dict)
def auto_assign_open_issues(
    limit: int = Query(50, ge=1, le=200, description="Max issues to process in one call"),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Trigger geo-routing auto-assignment for all unassigned open issues in the admin's scope.

    Useful after workers come online, or after a batch import of issues.
    Processes up to ``limit`` issues per call (default 50) to avoid timeouts.

    Returns:
        ``{"assigned": int, "skipped": int, "total_open": int}``

    **Roles**: any admin (scoped to their area).
    """
    from app.services.geo_service import auto_assign
    from app.services.notification_service import notify_localized

    try:
        query = (
            apply_admin_scope(db.query(Issue), current_user, Issue)
            .filter(
                Issue.status == "open",
                Issue.assigned_worker_id.is_(None),
            )
            .order_by(Issue.is_escalated.desc(), Issue.created_at.asc())
            .limit(limit)
        )
        query = apply_not_deleted_filter(query)
        open_issues = query.all()
        total_open = apply_admin_scope(db.query(func.count(Issue.id)), current_user, Issue).filter(
            Issue.status == "open", Issue.assigned_worker_id.is_(None)
        )
        total_open = apply_not_deleted_filter(total_open).scalar() or 0

        # Assign everything first, then commit once, then notify.
        #
        # This was one COMMIT per issue plus one notify (another commit, plus a
        # blocking FCM round-trip) inside the loop — up to 400 commits and 200
        # push calls in a single HTTP request at limit=200. Worse, the handler
        # below raised 500 with no rollback, so a failure at issue 137 left 136
        # issues assigned while telling the admin the operation had failed.
        assigned = []
        skipped_count = 0
        for issue in open_issues:
            if auto_assign(issue, db):
                assigned.append(issue)
            else:
                skipped_count += 1

        db.commit()
        assigned_count = len(assigned)

        # Notifications are best-effort and come after the state is durable, so
        # a push failure cannot undo the assignments.
        for issue in assigned:
            try:
                db.refresh(issue)
                if issue.assigned_worker:
                    notify_localized(
                        db=db,
                        user=issue.assigned_worker,
                        key="assignment",
                        notification_type="assignment",
                        issue_id=str(issue.id),
                        issue_type=issue.issue_type,
                        ward=issue.ward or "your area",
                    )
            except Exception as e:
                logger.warning("Assignment notification failed for issue %s: %s", issue.id, e)
    except Exception as e:
        db.rollback()
        logger.error(f"Auto-assign open issues error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Auto-assignment failed.")

    logger.info(
        f"Admin {current_user.id} triggered auto-assign: "
        f"{assigned_count} assigned, {skipped_count} skipped, {total_open} total open"
    )
    return {"assigned": assigned_count, "skipped": skipped_count, "total_open": total_open}


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Delete a user account (soft-delete/deactivation).
    
    FIX HIGH PRIORITY BUG #8: Admin can delete any user - Add role-based deletion checks
    
    Authorization rules:
    - super_admin: can delete any user
    - district_admin: can delete users in their district (workers and ward_admins)
    - taluka_admin: can delete users in their taluka (workers and ward_admins)
    - ward_admin: can delete workers in their ward only
    - Cannot delete other admins of equal or higher rank
    
    **Roles**: any admin.
    """
    if str(user_id) == str(current_user.id):
        raise ValidationError(
            "user_id",
            "Cannot delete your own account. Contact support to deactivate your account."
        )
    
    # Fetch the user to delete
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ResourceNotFoundError("User", user_id)
    
    # Authorization checks based on admin role and user role
    if current_user.role == "admin":  # super-admin
        # Super-admin can delete anyone
        pass
    elif user.role == "admin":  
        # No regular admin can delete other admins
        from app.core.exceptions import AuthorizationError
        raise AuthorizationError("delete", "admin user")
    elif current_user.role == "district_admin":
        # District admin can only delete workers and ward_admins in their district
        if user.role in ["admin", "district_admin", "taluka_admin"]:
            from app.core.exceptions import AuthorizationError
            raise AuthorizationError("delete", "higher-rank admin")
        
        # Check user is in their district.
        #
        # The `user.district_id and ...` guard these three checks used to carry
        # short-circuited to False whenever the column was NULL — and NULL is the
        # default for every account created through register, verify-otp, Google
        # or Aadhaar. So the jurisdiction check silently passed for essentially
        # all citizens, letting a ward_admin in one village deactivate any
        # citizen in the state. A user with no scope belongs to no jurisdiction,
        # so no scoped admin owns them: only the super-admin may delete them.
        if user.district_id != current_user.district_id:
            from app.core.exceptions import AuthorizationError
            raise AuthorizationError("delete", "user outside your jurisdiction")
    elif current_user.role == "taluka_admin":
        # Taluka admin can only delete workers and ward_admins in their taluka
        if user.role in ["admin", "district_admin", "taluka_admin"]:
            from app.core.exceptions import AuthorizationError
            raise AuthorizationError("delete", "higher-rank admin")

        # Check user is in their taluka (see the NULL-scope note above)
        if user.taluka_id != current_user.taluka_id:
            from app.core.exceptions import AuthorizationError
            raise AuthorizationError("delete", "user outside your jurisdiction")
    elif current_user.role == "ward_admin":
        # Ward admin can only delete workers in their ward
        if user.role != "worker":
            from app.core.exceptions import AuthorizationError
            raise AuthorizationError("delete", "non-worker user")

        # (see the NULL-scope note above)
        if user.ward_id != current_user.ward_id:
            from app.core.exceptions import AuthorizationError
            raise AuthorizationError("delete", "worker outside your ward")
    
    # Perform soft delete (deactivation)
    try:
        # Log the deletion action for audit purposes
        logger.warning(
            "Admin deleted user account",
            extra={
                "deleting_admin_id": str(current_user.id),
                "deleting_admin_role": current_user.role,
                "deleted_user_id": str(user_id),
                "deleted_user_role": user.role,
                "timestamp": now_utc().isoformat()
            }
        )
        
        # Mark as inactive and revoke tokens
        user.is_active = False
        # Clear any outstanding invitation so the account cannot reactivate
        # itself through the pending-worker branch in login/verify-otp, and
        # retire stateless access tokens that revocation does not reach.
        user.must_change_password = False
        user.invitation_sent_at = None
        user.tokens_valid_from = now_utc()
        revoke_all_user_tokens(str(user_id), db)
        
        # If worker, unassign pending tasks
        if user.role == "worker":
            pending = db.query(Issue).filter(
                Issue.assigned_worker_id == user_id,
                Issue.status.in_(["assigned", "in_progress"])
            ).all()
            for issue in pending:
                issue.assigned_worker_id = None
                issue.status = "open"
        
        db.commit()
        
        logger.info(
            f"User {user_id} ({user.role}) deleted by admin {current_user.id}",
            extra={
                "admin_role": current_user.role,
                "user_role": user.role,
                "pending_tasks_reassigned": len(pending) if user.role == "worker" else 0
            }
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting user {user_id}: {e}", exc_info=True)
        # Raw DB exception text was being forwarded to the client here.
        raise ProcessingError("user_deletion", "Failed to delete user. Please try again.")


@router.delete("/issues/{issue_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_issue(
    issue_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Soft-delete an issue (hides it from all views but retains the record).

    The citizen retains a record of their report; data is preserved for audit.
    Only super-admin can permanently see/restore deleted issues.

    **Roles**: any admin (scoped to their area).
    """
    issue = apply_admin_scope(db.query(Issue), current_user, Issue).filter(Issue.id == issue_id).first()
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    if issue.is_deleted:
        raise HTTPException(status_code=400, detail="Issue is already deleted")
    try:
        issue.is_deleted = True
        issue.deleted_at = now_utc()
        db.commit()
    except Exception as e:
        logger.error(f"Error soft-deleting issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete issue.")
    logger.info(f"Admin {current_user.id} soft-deleted issue {issue_id}")


# ── CSV Export ────────────────────────────────────────────────────────────────

@router.get("/issues/export")
def export_issues(
    status_filter: Optional[str] = Query(None, alias="status"),
    issue_type: Optional[str] = Query(None),
    ward: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Export issues as a downloadable CSV file (admin-scoped). **Roles**: any admin."""
    import csv
    import io
    from fastapi.responses import StreamingResponse

    try:
        since = now_utc() - timedelta(days=days)
        query = (
            apply_admin_scope(db.query(Issue), current_user, Issue)
            .filter(Issue.created_at >= since)
        )
        query = apply_not_deleted_filter(query)

        if status_filter:
            query = query.filter(Issue.status == status_filter)
        if issue_type:
            query = query.filter(Issue.issue_type == issue_type)
        if ward:
            query = query.filter(Issue.ward == ward)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "id", "issue_type", "severity", "priority", "status", "department",
            "ward", "address", "description", "upvote_count",
            "is_escalated", "is_blocked", "created_at", "resolved_at",
        ])
        count = 0
        for i in query.order_by(Issue.created_at.desc()).yield_per(500):
            writer.writerow([
                str(i.id), i.issue_type, i.severity, i.priority, i.status,
                i.department or "", i.ward or "", i.address or "",
                (i.description or "").replace("\n", " "),
                i.upvote_count, i.is_escalated, i.is_blocked,
                i.created_at.isoformat() if i.created_at else "",
                i.resolved_at.isoformat() if i.resolved_at else "",
            ])
            count += 1

        output.seek(0)
    except Exception as e:
        logger.error(f"Error exporting issues: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to export issues.")

    logger.info(f"Admin {current_user.id} exported {count} issues")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=issues_export.csv"},
    )


# ── Admin scope context ───────────────────────────────────────────────────────

@router.get("/me/scope")
def get_my_scope(
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Return the current admin's geographic scope context.

    Used by the frontend to display the admin's scope badge and to restrict
    which location management actions are visible.

    **Optimization**: Batch-load all location models instead of sequential queries.

    Returns::

        {
            "role": "district_admin",
            "district_id": "...", "district_name": "Rajkot",
            "taluka_id": null, "taluka_name": null,
            "ward_id": null, "ward_name": null,
        }
    """
    from app.models.location import District as DistrictModel, Taluka as TalukaModel, Ward as WardModel

    district_id = district_name = taluka_id = taluka_name = ward_id = ward_name = None

    # ── OPTIMIZATION: Batch-load all location IDs instead of sequential queries ──
    location_ids = {
        'district': current_user.district_id,
        'taluka': current_user.taluka_id,
        'ward': current_user.ward_id,
    }
    
    # Build a single batch query for all needed locations
    needed_ids = [v for v in location_ids.values() if v]
    if needed_ids:
        # Fetch all districts, talukas, and wards in one batch
        districts = {d.id: d for d in db.query(DistrictModel).all()}
        talukas = {t.id: t for t in db.query(TalukaModel).all()}
        wards = {w.id: w for w in db.query(WardModel).all()}
    else:
        districts = talukas = wards = {}

    # Extract data from cached objects
    if current_user.district_id:
        d = districts.get(current_user.district_id)
        district_id = str(current_user.district_id)
        district_name = d.name if d else None

    if current_user.taluka_id:
        t = talukas.get(current_user.taluka_id)
        taluka_id = str(current_user.taluka_id)
        taluka_name = t.name if t else None
        if not district_id and t:
            d = districts.get(t.district_id)
            district_id = str(t.district_id)
            district_name = d.name if d else None

    if current_user.ward_id:
        w = wards.get(current_user.ward_id)
        ward_id = str(current_user.ward_id)
        ward_name = w.name if w else None
        if not taluka_id and w:
            t = talukas.get(w.taluka_id)
            taluka_id = str(w.taluka_id)
            taluka_name = t.name if t else None
            if t and not district_id:
                d = districts.get(t.district_id)
                district_id = str(t.district_id)
                district_name = d.name if d else None

    return {
        "role": current_user.role,
        "district_id": district_id,
        "district_name": district_name,
        "taluka_id": taluka_id,
        "taluka_name": taluka_name,
        "ward_id": ward_id,
        "ward_name": ward_name,
    }


# ── Workers ───────────────────────────────────────────────────────────────────

@router.get("/workers", response_model=Dict)
def list_workers(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    is_online: Optional[bool] = Query(None),
    is_active: Optional[bool] = Query(None),
    pending_invite: Optional[bool] = Query(
        None,
        description="True lists workers who have been invited but have not signed in yet.",
    ),
    ward: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """List workers in the admin's scope. **Roles**: any admin.

    Query params:
      - is_active: true/false to list active or deactivated workers.
      - pending_invite: true lists workers who still hold the temporary password
        they were invited with — i.e. they have never signed in.

    `pending_invite` exists because the console's "Invited workers" tab used to
    approximate it with ``is_active=false``, which is a different set entirely:
    deactivated workers. The two never overlap, so that tab listed people who
    could not be re-invited while hiding everyone who was actually waiting on an
    invitation.
    """
    try:
        # Filter on the role directly. This used to call get_users_by_role(),
        # which ends in .all(), pull every worker row into Python, and send the
        # UUIDs straight back to Postgres as an IN list — which also blows past
        # the bind-parameter limit once the worker table is large.
        query = db.query(User).filter(User.role == "worker")
        if is_active is None:
            query = query.filter(User.is_active == True)  # noqa: E712

        if is_active is not None:
            query = query.filter(User.is_active == is_active)

        if pending_invite is not None:
            # An invitation is only outstanding while the account is still
            # usable; a deactivated worker holding a temporary password is not
            # waiting on anything, and offering to re-send to them would create
            # a working sign-in link for a disabled account.
            if pending_invite:
                query = query.filter(
                    User.must_change_password == True,  # noqa: E712
                    User.is_active == True,  # noqa: E712
                )
            else:
                query = query.filter(User.must_change_password == False)  # noqa: E712

        # Scope by location FK when available
        scope = user_scope_filter(current_user)
        if scope:
            query = query.filter(*scope)

        if is_online is not None:
            query = query.filter(User.is_online == is_online)
        if ward:
            query = query.filter(User.ward == ward)
        if department:
            query = query.filter(User.department == department)
        if search:
            like = f"%{search}%"
            query = query.filter((User.name.ilike(like)) | (User.phone.ilike(like)))

        total = query.count()
        workers = query.order_by(User.name).offset((page - 1) * size).limit(size).all()
    except Exception as e:
        logger.error(f"Error listing workers: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch workers.")

    return {
        "items": [UserResponse.model_validate(w).model_dump() for w in workers],
        "total": total, "page": page, "size": size,
    }


async def _try_send_invitation(*, to_email: str, worker_name: str, phone: str, temp_password: str) -> bool:
    """Send the invitation, converting any failure into a False return.

    The password hash is committed before this runs, so an exception escaping
    here would leave the worker holding a credential that no longer exists
    anywhere in readable form. Delivery is allowed to fail; losing the password
    is not.
    """
    try:
        return await send_worker_invitation(
            to_email=to_email,
            worker_name=worker_name,
            phone=phone,
            temp_password=temp_password,
        )
    except Exception as e:
        logger.error("Invitation email raised %s: %s", type(e).__name__, e, exc_info=True)
        return False


def _invitation_result(worker: User, temp_password: str, email_sent: bool) -> WorkerInvitationResult:
    """Build the invitation response, disclosing the password only when needed.

    Only the hash is stored, so if the worker did not receive the email this is
    the last place the password exists. The console email backend counts as "not
    delivered" — it logs a redacted message and sends nothing.
    """
    delivered = email_sent and settings.EMAIL_BACKEND != "console"
    if delivered:
        message = f"Invitation email sent to {worker.email}."
    elif settings.EMAIL_BACKEND == "console":
        message = (
            "EMAIL_BACKEND is 'console', so no email was sent. "
            "Give the temporary password below to the worker."
        )
    else:
        message = (
            "The invitation email could not be delivered. Give the temporary "
            "password below to the worker, or call the resend endpoint once "
            "email is working again."
        )
        logger.error(
            "Invitation email delivery failed for worker %s (%s)",
            worker.id,
            worker.email,
        )

    return WorkerInvitationResult(
        user=UserResponse.model_validate(worker),
        invitation_email_sent=delivered,
        temp_password=None if delivered else temp_password,
        message=message,
    )


@router.post("/workers", response_model=WorkerInvitationResult, status_code=status.HTTP_201_CREATED)
async def create_worker(
    body: CreateWorker,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Create a new worker account.

    Generates a temporary password and sends an invitation email to the worker.
    The worker starts as inactive (is_active=False) and must change the password
    within 7 days of receiving the invitation.

    ward_admin can only create workers for their own ward.
    taluka_admin/district_admin/admin can assign any ward.

    **Roles**: any admin.
    """

    try:
        # Check phone uniqueness
        existing_phone = db.query(User).filter(User.phone == body.phone).first()
        if existing_phone:
            raise HTTPException(status_code=409, detail="Phone number already registered")

        # Check email uniqueness
        existing_email = db.query(User).filter(User.email == body.email).first()
        if existing_email:
            raise HTTPException(status_code=409, detail="Email address already registered")

        ward_id = getattr(body, "ward_id", None)
        taluka_id = getattr(body, "taluka_id", None)
        district_id = getattr(body, "district_id", None)

        # Scope enforcement: each admin tier can only create workers within their jurisdiction
        if current_user.role == "ward_admin":
            ward_id = current_user.ward_id
            taluka_id = current_user.taluka_id
            district_id = current_user.district_id
        elif current_user.role == "taluka_admin":
            taluka_id = current_user.taluka_id
            district_id = current_user.district_id
        elif current_user.role == "district_admin":
            district_id = current_user.district_id

        # Auto-populate taluka_id and district_id from ward hierarchy
        if ward_id:
            from app.models.location import Ward
            ward_obj = db.query(Ward).filter(Ward.id == ward_id).first()
            if ward_obj:
                taluka_id = ward_obj.taluka_id
                # Get district from taluka
                from app.models.location import Taluka
                taluka_obj = db.query(Taluka).filter(Taluka.id == taluka_id).first()
                if taluka_obj:
                    district_id = taluka_obj.district_id

        # Generate temporary password (16-character URL-safe string)
        temp_password = secrets.token_urlsafe(12)

        # Create worker with temporary password and invitation fields
        worker = User(
            phone=body.phone,
            email=body.email,
            name=body.name,
            ward=body.ward,
            ward_id=ward_id,
            taluka_id=taluka_id,
            district_id=district_id,
            department=getattr(body, "department", None),
            role="worker",
            password_hash=hash_password(temp_password),
            is_active=False,  # Pending until first login
            must_change_password=True,
            invitation_sent_at=now_utc(),
        )
        db.add(worker)
        db.commit()
        db.refresh(worker)

        # Best-effort email: the account is already committed, so a delivery
        # failure must not lose the credential. The password is returned to the
        # calling admin instead of being logged — see WorkerInvitationResult.
        email_sent = await _try_send_invitation(
            to_email=body.email,
            worker_name=body.name or "",
            phone=body.phone,
            temp_password=temp_password,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating worker: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create worker account.")

    logger.info(f"Admin {current_user.id} created worker {worker.id} ({body.phone})")
    return _invitation_result(worker, temp_password, email_sent)


@router.post("/workers/{worker_id}/resend-invitation", response_model=WorkerInvitationResult)
async def resend_worker_invitation(
    worker_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Issue a fresh temporary password for a worker and re-send the invitation.

    The recovery path for a worker whose invitation email never arrived. Without
    it, a failed delivery left the account permanently unusable: the password is
    stored only as a hash, and the account is deactivated after 7 days of
    inactivity by the escalation job.

    Rotates the password, so any previously issued one stops working. Only
    applies to workers who have not yet completed their first login.

    **Roles**: any admin (scoped).

    Raises:
        404: Worker not found, or outside your jurisdiction.
        409: Worker has already set their own password.
    """
    query = db.query(User).filter(User.id == worker_id, User.role == "worker")
    scope = user_scope_filter(current_user)
    if scope:
        query = query.filter(*scope)
    worker = query.first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    if not worker.must_change_password:
        raise HTTPException(
            status_code=409,
            detail="This worker has already set their own password; use password reset instead.",
        )

    if not worker.email:
        raise HTTPException(status_code=409, detail="This worker has no email address on file.")

    try:
        temp_password = secrets.token_urlsafe(12)
        worker.password_hash = hash_password(temp_password)
        worker.invitation_sent_at = now_utc()
        db.commit()
        db.refresh(worker)

        email_sent = await _try_send_invitation(
            to_email=worker.email,
            worker_name=worker.name or "",
            phone=worker.phone,
            temp_password=temp_password,
        )
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error resending invitation for worker {worker_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to resend invitation.")

    logger.info(f"Admin {current_user.id} resent invitation for worker {worker.id}")
    return _invitation_result(worker, temp_password, email_sent)


@router.put("/workers/{worker_id}", response_model=UserResponse)
def update_worker(
    worker_id: uuid.UUID,
    body: UpdateWorker,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Update a worker's profile. **Roles**: any admin (scoped)."""
    query = db.query(User).filter(User.id == worker_id, User.role == "worker")
    scope = user_scope_filter(current_user)
    if scope:
        query = query.filter(*scope)
    worker = query.first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    try:
        if body.name is not None:
            worker.name = body.name
        if body.phone is not None:
            # Validate phone format
            if not re.match(r'^\+91\d{10}$', body.phone):
                raise HTTPException(status_code=400, detail="Phone must be in format +91XXXXXXXXXX")
            # Check for uniqueness if changed
            if body.phone != worker.phone:
                existing = db.query(User).filter(User.phone == body.phone).first()
                if existing:
                    raise HTTPException(status_code=409, detail="Phone number already in use")
                worker.phone = body.phone
        if body.ward is not None:
            worker.ward = body.ward
        if body.ward_id is not None:
            worker.ward_id = body.ward_id
            # Auto-populate taluka_id and district_id from ward hierarchy
            from app.models.location import Ward, Taluka
            ward_obj = db.query(Ward).filter(Ward.id == body.ward_id).first()
            if ward_obj:
                worker.taluka_id = ward_obj.taluka_id
                taluka_obj = db.query(Taluka).filter(Taluka.id == ward_obj.taluka_id).first()
                if taluka_obj:
                    worker.district_id = taluka_obj.district_id
        if body.department is not None:
            worker.department = body.department
        if body.is_active is not None:
            worker.is_active = body.is_active
        db.commit()
        db.refresh(worker)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating worker {worker_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update worker.")

    logger.info(f"Admin {current_user.id} updated worker {worker_id}")
    return UserResponse.model_validate(worker)


@router.post("/workers/{worker_id}/deactivate", response_model=UserResponse)
def deactivate_worker(
    worker_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Deactivate a worker account. **Roles**: any admin (scoped)."""
    worker = _get_scoped_worker(worker_id, current_user, db)
    try:
        worker.is_active = False
        worker.is_online = False
        worker.is_available = False
        # Retire the outstanding invitation, if any. The login and verify-otp
        # paths reactivate a pending worker who still has one, so leaving these
        # set let a deactivated worker sign in and undo this.
        worker.must_change_password = False
        worker.invitation_sent_at = None
        # And retire their live access tokens, which are stateless and would
        # otherwise keep working until they expired.
        worker.tokens_valid_from = now_utc()
        revoke_all_user_tokens(str(worker_id), db)
        db.commit()
        db.refresh(worker)
    except Exception as e:
        db.rollback()
        logger.error(f"Error deactivating worker {worker_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to deactivate worker.")
    logger.info(f"Admin {current_user.id} deactivated worker {worker_id}")
    return UserResponse.model_validate(worker)


@router.post("/workers/{worker_id}/reactivate", response_model=UserResponse)
def reactivate_worker(
    worker_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Reactivate a worker account. **Roles**: any admin (scoped)."""
    worker = _get_scoped_worker(worker_id, current_user, db)
    try:
        worker.is_active = True
        db.commit()
        db.refresh(worker)
    except Exception as e:
        logger.error(f"Error reactivating worker {worker_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to reactivate worker.")
    logger.info(f"Admin {current_user.id} reactivated worker {worker_id}")
    return UserResponse.model_validate(worker)


@router.get("/workers/leaderboard", response_model=List[Dict])
def worker_leaderboard(
    limit: int = Query(10, ge=1, le=50),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Top workers ranked by performance score. **Roles**: any admin."""
    try:
        # See the note in list_workers — direct role filter, no round-trip.
        query = db.query(User).filter(User.role == "worker", User.is_active == True)  # noqa: E712
        scope = user_scope_filter(current_user)
        if scope:
            query = query.filter(*scope)
        workers = query.all()

        board = []
        # Pre-load ward names for all workers to avoid N+1
        from app.models.location import Ward as WardModel
        ward_ids = {w.ward_id for w in workers if w.ward_id}
        ward_map = {}
        if ward_ids:
            ward_rows = db.query(WardModel).filter(WardModel.id.in_(ward_ids)).all()
            ward_map = {str(wr.id): f"{wr.name} (Ward-{wr.ward_number})" for wr in ward_rows}

        # Pre-fetch all issue stats in a single aggregated query
        worker_ids = [w.id for w in workers]
        stats_rows = (
            db.query(
                Issue.assigned_worker_id,
                Issue.status,
                func.count(Issue.id).label("cnt"),
                func.avg(Issue.citizen_rating).label("avg_r"),
            )
            .filter(Issue.assigned_worker_id.in_(worker_ids))
            .group_by(Issue.assigned_worker_id, Issue.status)
            .all()
        )

        # Build a per-worker stats dict
        from collections import defaultdict as _dd
        worker_stats = _dd(lambda: {"resolved": 0, "in_progress": 0, "closed": 0, "total": 0, "rating_sum": 0, "rating_cnt": 0})
        for row in stats_rows:
            ws = worker_stats[row.assigned_worker_id]
            ws["total"] += row.cnt
            # Count both "resolved" and "closed" as completed tasks
            if row.status == "resolved" or row.status == "closed":
                ws["resolved"] += row.cnt
            elif row.status == "in_progress":
                ws["in_progress"] += row.cnt

        # Fetch avg rating per worker in one query
        rating_rows = (
            db.query(Issue.assigned_worker_id, func.avg(Issue.citizen_rating))
            .filter(Issue.assigned_worker_id.in_(worker_ids), Issue.citizen_rating.isnot(None))
            .group_by(Issue.assigned_worker_id)
            .all()
        )
        rating_map = {row[0]: round(float(row[1]), 2) for row in rating_rows}

        for w in workers:
            ws = worker_stats[w.id]
            avg_r = rating_map.get(w.id)
            # Calculate resolution rate based on completed tasks (resolved + closed)
            completed = ws["resolved"]  # Now includes both resolved and closed
            resolution_rate = round((completed / ws["total"]) * 100, 1) if ws["total"] > 0 else 0.0
            board.append({
                "worker_id": str(w.id),
                "name": w.name or w.phone,
                "phone": w.phone,
                "ward": w.ward or ward_map.get(str(w.ward_id)),
                "department": w.department,
                "is_online": w.is_online,
                "is_available": w.is_available,
                "tasks_resolved": completed,
                "tasks_total": ws["total"],
                "tasks_in_progress": ws["in_progress"],
                "resolution_rate": resolution_rate,
                "avg_rating": avg_r,
                "score": round(completed * (avg_r or 3.0), 1),
            })
        board.sort(key=lambda x: x["score"], reverse=True)
    except Exception as e:
        logger.error(f"Error fetching leaderboard: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch leaderboard.")
    return board[:limit]


@router.get("/workers/locations", response_model=List[Dict])
def get_all_worker_locations(
    online_only: bool = Query(True),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Live locations of all workers for the admin map. **Roles**: any admin."""
    try:
        # See the note in list_workers — direct role filter, no round-trip.
        query = db.query(User).filter(User.role == "worker", User.is_active == True)  # noqa: E712
        scope = user_scope_filter(current_user)
        if scope:
            query = query.filter(*scope)
        if online_only:
            query = query.filter(User.is_online == True)
        result = [
            {
                "worker_id": str(w.id),
                "name": w.name or w.phone,
                "ward": w.ward,
                "department": w.department,
                "is_online": w.is_online,
                "is_available": w.is_available,
                "latitude": w.latitude,
                "longitude": w.longitude,
                "location_updated_at": w.location_updated_at,
            }
            for w in query.all()
            if w.latitude is not None and w.longitude is not None
        ]
    except Exception as e:
        logger.error(f"Error fetching worker locations: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch worker locations.")
    return result


@router.get("/workers/{worker_id}", response_model=Dict)
def get_worker(
    worker_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Get a single worker's full profile with task statistics. **Roles**: any admin."""
    worker = _get_scoped_worker(worker_id, current_user, db)
    try:
        resolved = db.query(func.count(Issue.id)).filter(
            Issue.assigned_worker_id == worker.id, Issue.status == "resolved"
        ).scalar() or 0
        pending = db.query(func.count(Issue.id)).filter(
            Issue.assigned_worker_id == worker.id, Issue.status.in_(["assigned", "in_progress"])
        ).scalar() or 0
        avg_rating = db.query(func.avg(Issue.citizen_rating)).filter(
            Issue.assigned_worker_id == worker.id, Issue.citizen_rating.isnot(None)
        ).scalar()
    except Exception as e:
        logger.error(f"Error fetching worker {worker_id} stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch worker details.")

    profile = UserResponse.model_validate(worker).model_dump()
    profile["stats"] = {
        "tasks_resolved": resolved,
        "tasks_pending": pending,
        "avg_rating": round(float(avg_rating), 2) if avg_rating else None,
    }
    return profile


@router.get("/workers/{worker_id}/location", response_model=Dict)
def get_worker_location(
    worker_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Get last known GPS location of a worker. **Roles**: any admin."""
    worker = _get_scoped_worker(worker_id, current_user, db)
    return {
        "worker_id": str(worker.id),
        "name": worker.name,
        "is_online": worker.is_online,
        "latitude": worker.latitude,
        "longitude": worker.longitude,
        "location_updated_at": worker.location_updated_at,
    }


@router.get("/workers/{worker_id}/report", response_model=Dict)
def get_worker_performance_report(
    worker_id: uuid.UUID,
    days: int = Query(30, ge=1, le=365, description="Number of days to look back"),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Period-based performance report for a single worker.

    Returns task counts, resolution rate, avg rating, avg resolution time,
    and a daily breakdown of resolved tasks for the period.

    **Roles**: any admin (scoped).
    """
    from sqlalchemy import extract
    worker = _get_scoped_worker(worker_id, current_user, db)
    since = now_utc() - timedelta(days=days)

    try:
        base = db.query(Issue).filter(
            Issue.assigned_worker_id == worker.id,
            Issue.created_at >= since,
        )

        total_assigned = base.count()
        resolved = base.filter(Issue.status == "resolved").count()
        in_progress = base.filter(Issue.status == "in_progress").count()
        rejected = base.filter(Issue.reassignment_count > 0).count()

        avg_resolution_secs = (
            base.filter(Issue.status == "resolved", Issue.resolved_at.isnot(None))
            .with_entities(
                func.avg(extract('epoch', Issue.resolved_at) - extract('epoch', Issue.created_at))
            )
            .scalar()
        )
        avg_resolution_hours = round(float(avg_resolution_secs) / 3600, 1) if avg_resolution_secs else None

        avg_rating_val = (
            base.filter(Issue.citizen_rating.isnot(None))
            .with_entities(func.avg(Issue.citizen_rating))
            .scalar()
        )
        avg_rating = round(float(avg_rating_val), 2) if avg_rating_val else None

        five_star_count = base.filter(Issue.citizen_rating == 5).count()
        resolution_rate = round((resolved / total_assigned) * 100, 1) if total_assigned else 0.0

        # Daily breakdown of resolved tasks over the period
        daily_rows = (
            db.query(
                func.date(Issue.resolved_at).label("day"),
                func.count(Issue.id).label("count"),
            )
            .filter(
                Issue.assigned_worker_id == worker.id,
                Issue.status == "resolved",
                Issue.resolved_at >= since,
            )
            .group_by(func.date(Issue.resolved_at))
            .order_by(func.date(Issue.resolved_at))
            .all()
        )
        daily_resolved = [{"date": str(row.day), "resolved": row.count} for row in daily_rows]

        # Issue breakdown by type
        type_rows = (
            base.filter(Issue.status == "resolved")
            .with_entities(Issue.issue_type, func.count(Issue.id))
            .group_by(Issue.issue_type)
            .all()
        )
        by_type = {row[0]: row[1] for row in type_rows}

    except Exception as e:
        logger.error(f"Error generating performance report for worker {worker_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to generate worker report.")

    return {
        "worker_id": str(worker.id),
        "name": worker.name or worker.phone,
        "ward": worker.ward,
        "department": worker.department,
        "period_days": days,
        "total_assigned": total_assigned,
        "resolved": resolved,
        "in_progress": in_progress,
        "rejected_count": rejected,
        "resolution_rate": resolution_rate,
        "avg_resolution_hours": avg_resolution_hours,
        "avg_citizen_rating": avg_rating,
        "five_star_count": five_star_count,
        "by_issue_type": by_type,
        "daily_resolved": daily_resolved,
    }


# ── Citizens ──────────────────────────────────────────────────────────────────

@router.get("/citizens", response_model=Dict)
def list_citizens(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    search: Optional[str] = Query(None),
    ward: Optional[str] = Query(None),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """List citizens in the admin's scope (paginated). **Roles**: any admin."""
    try:
        query = db.query(User).filter(User.role == "citizen")
        scope = user_scope_filter(current_user)
        if scope:
            query = query.filter(*scope)
        if ward:
            query = query.filter(User.ward == ward)
        if search:
            like = f"%{search}%"
            query = query.filter(
                (User.name.ilike(like)) | (User.phone.ilike(like)) | (User.email.ilike(like))
            )

        total = query.count()
        citizens = query.order_by(User.created_at.desc()).offset((page - 1) * size).limit(size).all()

        # Batch fetch issue counts for all citizens on this page (avoid N+1)
        citizen_ids = [c.id for c in citizens]
        issue_count_rows = (
            db.query(Issue.reporter_id, func.count(Issue.id))
            .filter(Issue.reporter_id.in_(citizen_ids))
            .group_by(Issue.reporter_id)
            .all()
        ) if citizen_ids else []
        issue_counts = {row[0]: row[1] for row in issue_count_rows}

        items = []
        for c in citizens:
            items.append({
                "id": str(c.id),
                "name": c.name,
                "phone": c.phone,
                "email": c.email,
                "ward": c.ward,
                "language": c.language,
                "is_active": c.is_active,
                "google_linked": c.google_id is not None,
                "aadhar_verified": c.aadhar_verified,
                "total_issues_reported": issue_counts.get(c.id, 0),
                "created_at": c.created_at.isoformat() if c.created_at else None,
            })
    except Exception as e:
        logger.error(f"Error listing citizens: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch citizens.")
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/citizens/{citizen_id}", response_model=Dict)
def get_citizen(
    citizen_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Get citizen profile with issue statistics. **Roles**: any admin (scoped)."""
    query = db.query(User).filter(User.id == citizen_id, User.role == "citizen")
    scope = user_scope_filter(current_user)
    if scope:
        query = query.filter(*scope)
    citizen = query.first()
    if not citizen:
        raise HTTPException(status_code=404, detail="Citizen not found")
    try:
        total_reported = db.query(func.count(Issue.id)).filter(Issue.reporter_id == citizen.id).scalar() or 0
        total_resolved = db.query(func.count(Issue.id)).filter(
            Issue.reporter_id == citizen.id, Issue.status == "resolved"
        ).scalar() or 0
        total_open = db.query(func.count(Issue.id)).filter(
            Issue.reporter_id == citizen.id, Issue.status.in_(["open", "assigned", "in_progress"])
        ).scalar() or 0
    except Exception as e:
        logger.error(f"Error fetching citizen {citizen_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch citizen details.")
    return {
        "id": str(citizen.id),
        "name": citizen.name,
        "phone": citizen.phone,
        "email": citizen.email,
        "ward": citizen.ward,
        "language": citizen.language,
        "is_active": citizen.is_active,
        "google_linked": citizen.google_id is not None,
        "aadhar_verified": citizen.aadhar_verified,
        "created_at": citizen.created_at.isoformat() if citizen.created_at else None,
        "stats": {"total_reported": total_reported, "total_resolved": total_resolved, "total_open": total_open},
    }


@router.post("/citizens/{citizen_id}/deactivate", response_model=Dict)
def deactivate_citizen(
    citizen_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Deactivate a citizen account. **Roles**: any admin."""
    query = db.query(User).filter(User.id == citizen_id, User.role == "citizen")
    scope = user_scope_filter(current_user)
    if scope:
        query = query.filter(*scope)
    citizen = query.first()
    if not citizen:
        raise HTTPException(status_code=404, detail="Citizen not found")
    try:
        citizen.is_active = False
        db.commit()
    except Exception as e:
        logger.error(f"Error deactivating citizen {citizen_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to deactivate citizen.")
    logger.info(f"Admin {current_user.id} deactivated citizen {citizen_id}")
    return {"id": str(citizen.id), "is_active": False}


@router.post("/citizens/{citizen_id}/reactivate", response_model=Dict)
def reactivate_citizen(
    citizen_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Reactivate a citizen account. **Roles**: any admin."""
    query = db.query(User).filter(User.id == citizen_id, User.role == "citizen")
    scope = user_scope_filter(current_user)
    if scope:
        query = query.filter(*scope)
    citizen = query.first()
    if not citizen:
        raise HTTPException(status_code=404, detail="Citizen not found")
    try:
        citizen.is_active = True
        db.commit()
    except Exception as e:
        logger.error(f"Error reactivating citizen {citizen_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to reactivate citizen.")
    logger.info(f"Admin {current_user.id} reactivated citizen {citizen_id}")
    return {"id": str(citizen.id), "is_active": True}


# ── Sub-admin management ──────────────────────────────────────────────────────

class CreateSubAdminRequest(BaseModel):
    """Request body for creating a sub-admin account."""
    phone: str = Field(..., examples=["+919876543210"])
    name: str = Field(..., min_length=2)
    role: str = Field(..., description="ward_admin | taluka_admin | district_admin")
    ward_id: Optional[uuid.UUID] = Field(None, description="Required for ward_admin")
    taluka_id: Optional[uuid.UUID] = Field(None, description="Required for taluka_admin/ward_admin")
    district_id: Optional[uuid.UUID] = Field(None, description="Required for district_admin+")
    language: str = Field("en")


@router.post("/admins", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_sub_admin(
    body: CreateSubAdminRequest,
    current_user: User = Depends(require_role("admin", "district_admin", "taluka_admin")),
    db: Session = Depends(get_db),
):
    """Create a sub-admin (ward_admin / taluka_admin / district_admin).

    Hierarchy rules:
    - ``admin`` (super) can create any sub-admin role.
    - ``district_admin`` can create ``taluka_admin`` and ``ward_admin`` in their district.
    - ``taluka_admin`` can create ``ward_admin`` in their taluka.
    - ``ward_admin`` cannot create any admins.

    ADMIN ROLES AUDIT - GAP #2: Sub-Admin Creation Validation
    Enforces strict role hierarchy to prevent privilege escalation.

    **Roles**: admin, district_admin, taluka_admin.
    """
    # ADMIN AUDIT FIX: Explicit hierarchy validation - prevent lower-level admins from creating higher roles
    allowed_role_creation = {
        "admin": {"ward_admin", "taluka_admin", "district_admin"},  # super-admin can create all
        "district_admin": {"taluka_admin", "ward_admin"},           # district can create taluka/ward
        "taluka_admin": {"ward_admin"},                             # taluka can only create ward
        # ward_admin is intentionally excluded - cannot create admins
    }
    
    # Check if current user's role is allowed to create admins at all
    if current_user.role not in allowed_role_creation:
        raise HTTPException(
            status_code=403,
            detail=f"Your role ({current_user.role}) cannot create admin accounts."
        )
    
    # Check if attempted role is in the allowed set for current user
    if body.role not in allowed_role_creation.get(current_user.role, set()):
        raise HTTPException(
            status_code=403,
            detail=f"Your role ({current_user.role}) cannot create a {body.role}. "
                   f"Allowed roles: {', '.join(allowed_role_creation.get(current_user.role, set())) or 'none'}"
        )

    try:
        existing = db.query(User).filter(User.phone == body.phone).first()
        if existing:
            raise HTTPException(status_code=409, detail="Phone number already registered")

        sub_admin = User(
            phone=body.phone,
            name=body.name,
            role=body.role,
            language=body.language,
            ward_id=body.ward_id,
            taluka_id=body.taluka_id or current_user.taluka_id,
            district_id=body.district_id or current_user.district_id,
            is_active=True,
        )
        db.add(sub_admin)
        db.commit()
        db.refresh(sub_admin)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating sub-admin: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create sub-admin account.")

    logger.info(f"Admin {current_user.id} ({current_user.role}) created {body.role}: {sub_admin.id}")
    return UserResponse.model_validate(sub_admin)


@router.get("/admins", response_model=Dict)
def list_sub_admins(
    role: Optional[str] = Query(None, description="Filter by role: ward_admin/taluka_admin/district_admin"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    current_user: User = Depends(require_role("admin", "district_admin", "taluka_admin")),
    db: Session = Depends(get_db),
):
    """List sub-admins in the current admin's scope. **Roles**: admin, district_admin, taluka_admin."""
    from app.core.deps import ADMIN_ROLES
    query = db.query(User).filter(User.role.in_(list(ADMIN_ROLES - {"admin"})))
    scope = user_scope_filter(current_user)
    if scope:
        query = query.filter(*scope)
    if role:
        query = query.filter(User.role == role)
    total = query.count()
    admins = query.order_by(User.created_at.desc()).offset((page - 1) * size).limit(size).all()
    return {
        "items": [UserResponse.model_validate(a).model_dump() for a in admins],
        "total": total, "page": page, "size": size,
    }


@router.put("/admins/{admin_id}", response_model=UserResponse)
def update_admin(
    admin_id: uuid.UUID,
    body: UpdateSubAdmin,
    current_user: User = Depends(require_role("admin", "district_admin", "taluka_admin")),
    db: Session = Depends(get_db),
):
    """Update an admin's profile (name, phone, role, scope). **Roles**: admin and above."""
    admin = db.query(User).filter(User.id == admin_id, User.role.in_(["district_admin", "taluka_admin", "ward_admin"])).first()
    if not admin:
        raise HTTPException(status_code=404, detail="Admin not found")

    # No self-editing. A sub-admin satisfies their own scope filter and holds a
    # role in the allowed list, so without this a taluka_admin could target their
    # own id and rewrite their jurisdiction — the scope check below would pass,
    # because it is checking them against themselves.
    if admin.id == current_user.id:
        raise HTTPException(
            status_code=403,
            detail="You cannot edit your own admin record. Ask a higher-level admin.",
        )

    # Check if current user has permission to edit this admin
    scope = user_scope_filter(current_user)
    if scope:
        # Apply scope filter to check permission
        user_query = db.query(User).filter(User.id == admin_id)
        user_query = user_query.filter(*scope)
        if not user_query.first():
            raise HTTPException(status_code=403, detail="You don't have permission to edit this admin")

    # Hierarchy validation for role change
    if body.role and body.role != admin.role:
        allowed_role_creation = {
            "admin": {"ward_admin", "taluka_admin", "district_admin"},
            "district_admin": {"taluka_admin", "ward_admin"},
            "taluka_admin": {"ward_admin"},
        }
        if body.role not in allowed_role_creation.get(current_user.role, set()):
            raise HTTPException(
                status_code=403,
                detail=f"Your role ({current_user.role}) cannot set an admin to {body.role}.",
            )

    try:
        if body.name is not None:
            admin.name = body.name
        if body.phone is not None:
            # Validate phone format
            if not re.match(r'^\+91\d{10}$', body.phone):
                raise HTTPException(status_code=400, detail="Phone must be in format +91XXXXXXXXXX")
            # Check for uniqueness if changed
            if body.phone != admin.phone:
                existing = db.query(User).filter(User.phone == body.phone).first()
                if existing:
                    raise HTTPException(status_code=409, detail="Phone number already in use")
                admin.phone = body.phone
        if body.role is not None:
            admin.role = body.role

        # The new scope must sit inside the caller's own. Checked before any of
        # the three columns is written, so a request naming a valid ward and an
        # out-of-jurisdiction district cannot land half-applied.
        _assert_scope_within_caller(
            current_user,
            ward_id=body.ward_id,
            taluka_id=body.taluka_id,
            district_id=body.district_id,
            db=db,
        )
        if body.ward_id is not None:
            admin.ward_id = body.ward_id
        if body.taluka_id is not None:
            admin.taluka_id = body.taluka_id
        if body.district_id is not None:
            admin.district_id = body.district_id

        db.commit()
        db.refresh(admin)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating admin {admin_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update admin.")

    logger.info(f"Admin {current_user.id} updated admin {admin_id}")
    return UserResponse.model_validate(admin)


# ── Role management ───────────────────────────────────────────────────────────

@router.patch("/users/{user_id}/role", response_model=UserResponse)
def change_user_role(
    user_id: uuid.UUID,
    role: str = Query(..., description="New role: citizen/worker/ward_admin/taluka_admin/district_admin/admin"),
    current_user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Change a user's role. **Roles**: super-admin only."""
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found or inactive")
    if str(user.id) == str(current_user.id):
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    valid_roles = {"citizen", "worker", "ward_admin", "taluka_admin", "district_admin", "admin"}
    if role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(valid_roles)}")

    try:
        old_role = user.role
        user.role = role
        db.commit()
        db.refresh(user)
    except Exception as e:
        logger.error(f"Error changing role for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to change user role.")

    logger.info(f"Admin {current_user.id} changed user {user_id} role: {old_role} → {role}")
    return UserResponse.model_validate(user)


# ── Admin Override Access ──────────────────────────────────────────────────────
# ADMIN ROLES AUDIT - GAP #3: Cross-Admin Data Visibility Override
# Allows super-admins to grant temporary access to data outside normal scope with audit logging

class GrantOverrideRequest(BaseModel):
    """Request to grant override access to another admin."""
    target_admin_id: uuid.UUID = Field(..., description="Admin to grant override to")
    scope_level: str = Field(..., description="Scope level: 'ward', 'taluka', 'district'")
    target_scope_id: uuid.UUID = Field(..., description="UUID of the location/scope")
    reason: str = Field(..., description="Reason for override (incident ID, emergency, etc.)")
    duration_minutes: Optional[int] = Field(None, description="How long override lasts (None = permanent)")


@router.post("/overrides/grant")
def grant_override_access(
    body: GrantOverrideRequest,
    current_user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Grant an admin temporary access to data outside their normal geographic scope.
    
    Incident Response Use Case:
    - Ward 1 admin needs to coordinate with Ward 2 during emergency
    - Super-admin grants temporary override to access Ward 2 data
    - All access is logged for compliance/audit trail
    
    **Roles**: super-admin only.
    """
    from app.models import AdminOverride
    
    # Verify target admin exists
    target_admin = db.query(User).filter(
        User.id == body.target_admin_id,
        User.role.in_(["ward_admin", "taluka_admin", "district_admin"])
    ).first()
    if not target_admin:
        raise HTTPException(status_code=404, detail="Target admin not found")
    
    # Validate scope level
    if body.scope_level not in ["ward", "taluka", "district"]:
        raise HTTPException(status_code=400, detail="Invalid scope_level. Must be 'ward', 'taluka', or 'district'")
    
    try:
        # Calculate override expiry time
        override_until = None
        if body.duration_minutes:
            override_until = now_utc() + timedelta(minutes=body.duration_minutes)
        
        # Create the AdminOverride entry
        override = AdminOverride(
            id=uuid.uuid4(),
            granted_by_admin_id=current_user.id,
            granted_to_admin_id=body.target_admin_id,
            scope_level=body.scope_level,
            target_scope_id=body.target_scope_id,
            reason=body.reason,
            override_until=override_until,
        )
        
        db.add(override)
        db.commit()
        db.refresh(override)
        
        # Also log to audit trail
        log_override_access(
            admin=target_admin,
            target_scope=f"{body.scope_level}:{body.target_scope_id}",
            reason=body.reason,
            resource_type="override_grant",
            duration_minutes=body.duration_minutes,
            db=db
        )
        
        logger.info(
            f"Super-admin {current_user.id} granted override to {target_admin.id} "
            f"for {body.scope_level}:{body.target_scope_id}. Reason: {body.reason}",
            extra={"audit": True}
        )
        
        return {
            "status": "success",
            "override_id": str(override.id),
            "message": f"Override granted until {override.override_until or 'manually revoked'}",
            "target_admin": target_admin.name,
            "scope_level": body.scope_level,
            "target_scope_id": str(body.target_scope_id),
            "expires_at": override.override_until.isoformat() if override.override_until else None,
        }
    except Exception as e:
        logger.error(f"Error granting override: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to grant override access")


@router.get("/overrides/active")
def list_active_overrides(
    admin_id: Optional[uuid.UUID] = Query(None, description="Filter by admin"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
    current_user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """List all active override access grants.
    
    **Roles**: super-admin only.
    """
    from app.models import AdminOverride, User as UserModel
    
    try:
        # Build query for active overrides
        query = db.query(AdminOverride).filter(
            AdminOverride.revoked_at.is_(None),  # Not revoked
            (AdminOverride.override_until.is_(None)) |  # Permanent or
            (AdminOverride.override_until > now_utc())  # Not expired
        )
        
        if admin_id:
            query = query.filter(AdminOverride.granted_to_admin_id == admin_id)
        
        # Count total
        total = query.count()
        
        # Apply pagination
        overrides = query.order_by(AdminOverride.created_at.desc()).offset((page - 1) * size).limit(size).all()
        
        # Build response with admin names
        override_list = []
        for o in overrides:
            # Get admin names
            granted_to = db.query(UserModel).filter(UserModel.id == o.granted_to_admin_id).first()
            
            override_list.append({
                "id": str(o.id),
                "admin_name": granted_to.name if granted_to else "Unknown",
                "target_scope": f"{o.scope_level}:{o.target_scope_id}",
                "reason": o.reason,
                "override_until": o.override_until.isoformat() if o.override_until else None,
            })
        
        return {
            "status": "success",
            "count": len(override_list),
            "total": total,
            "page": page,
            "size": size,
            "items": override_list
        }
    except Exception as e:
        logger.error(f"Error listing active overrides: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch active overrides")


@router.post("/overrides/{override_id}/revoke")
def revoke_admin_override(
    override_id: uuid.UUID,
    current_user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Revoke an active override access grant.
    
    **Roles**: super-admin only.
    """
    from app.models import AdminOverride
    
    try:
        override = db.query(AdminOverride).filter(AdminOverride.id == override_id).first()
        if not override:
            raise HTTPException(status_code=404, detail="Override not found")
        
        if override.revoked_at:
            raise HTTPException(status_code=400, detail="Override is already revoked")
        
        # Update revocation fields
        override.revoked_at = now_utc()
        override.revoked_by = current_user.id
        db.commit()
        db.refresh(override)
        
        logger.info(
            f"Super-admin {current_user.id} revoked override {override_id} "
            f"for admin {override.granted_to_admin_id}",
            extra={"audit": True}
        )
        
        return {
            "status": "success",
            "message": "Override successfully revoked",
            "override_id": str(override_id),
            "revoked_at": override.revoked_at.isoformat()
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error revoking override: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to revoke override")


@router.get("/overrides/audit-log")
def get_override_audit_log(
    admin_id: Optional[uuid.UUID] = Query(None, description="Filter by admin"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(50, ge=1, le=100, description="Page size"),
    current_user: User = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Get audit log of all override access (active and revoked).
    
    **Roles**: super-admin only.
    """
    from app.services.admin_override import AdminOverrideLog
    
    try:
        # Build query for audit logs
        query = db.query(AdminOverrideLog).order_by(AdminOverrideLog.created_at.desc())
        
        if admin_id:
            query = query.filter(AdminOverrideLog.admin_id == admin_id)
        
        # Count total
        total = query.count()
        
        # Apply pagination
        logs = query.offset((page - 1) * size).limit(size).all()
        
        return {
            "status": "success",
            "count": len(logs),
            "total": total,
            "page": page,
            "size": size,
            "items": [
                {
                    "id": str(o.id),
                    "admin_id": str(o.admin_id),
                    "admin_role": o.admin_role,
                    "target_scope": o.target_scope,
                    "accessed_resource_type": o.accessed_resource_type,
                    "reason": o.reason,
                    "created_at": o.created_at.isoformat(),
                    "expires_at": o.override_until.isoformat() if o.override_until else None,
                    "is_active": o.override_until is None or o.override_until > now_utc()
                }
                for o in logs
            ]
        }
    except Exception as e:
        logger.error(f"Error fetching override audit log: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch audit log")


# ── Admin Messaging ────────────────────────────────────────────────────────────
# ADMIN ROLES AUDIT - GAP #1: Admin-to-Admin Communication
# Messaging system for inter-admin coordination across hierarchy

class SendMessageRequest(BaseModel):
    """Request to send a message between admins."""
    receiver_id: uuid.UUID = Field(..., description="ID of receiving admin")
    subject: str = Field(..., min_length=1, max_length=255)
    body: str = Field(..., min_length=1, max_length=5000)
    message_type: str = Field("general", description="general | issue | worker | incident | escalation")
    is_urgent: str = Field("normal", description="normal | urgent | critical")
    related_resource_type: Optional[str] = Field(None, description="issue | worker | incident")
    related_resource_id: Optional[uuid.UUID] = Field(None, description="ID of related resource")


@router.post("/messages")
def send_message(
    body: SendMessageRequest,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Send a message to another admin.
    
    **Roles**: any admin.
    """
    try:
        from app.services.admin_messaging import send_admin_message
        
        # Verify receiver exists and is an admin
        receiver = db.query(User).filter(
            User.id == body.receiver_id,
            User.role.in_(["ward_admin", "taluka_admin", "district_admin", "admin"])
        ).first()
        if not receiver:
            raise HTTPException(status_code=404, detail="Recipient not found or is not an admin")
        
        if body.receiver_id == current_user.id:
            raise HTTPException(status_code=400, detail="Cannot send message to yourself")
        
        # Send message
        message = send_admin_message(
            sender=current_user,
            receiver_id=body.receiver_id,
            subject=body.subject,
            body=body.body,
            message_type=body.message_type,
            related_resource_type=body.related_resource_type,
            related_resource_id=body.related_resource_id,
            is_urgent=body.is_urgent,
            db=db
        )
        
        return {
            "status": "success",
            "message_id": str(message.id),
            "receiver": receiver.name,
            "sent_at": message.created_at.isoformat(),
            "subject": body.subject,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending admin message: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to send message")


@router.get("/messages/inbox")
def get_inbox_messages(
    unread_only: bool = Query(False, description="Show unread messages only"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Get inbox messages for current admin.
    
    **Roles**: any admin.
    """
    try:
        from app.services.admin_messaging import count_inbox, get_inbox

        # Count and page in SQL. This used to fetch every message the admin had
        # ever received and slice the list in Python — then touch m.sender.name
        # on each row, one query per message.
        total = count_inbox(current_user.id, db, unread_only=unread_only)
        messages = get_inbox(
            current_user.id, unread_only=unread_only, db=db,
            limit=size, offset=(page - 1) * size,
        )
        
        return {
            "status": "success",
            "total": total,
            "page": page,
            "size": size,
            "messages": [
                {
                    "id": str(m.id),
                    "from": m.sender.name if m.sender else None,
                    "sender_role": m.sender.role if m.sender else None,
                    "subject": m.subject,
                    "preview": m.body[:100] + "..." if len(m.body) > 100 else m.body,
                    "message_type": m.message_type,
                    "is_urgent": m.is_urgent,
                    "is_read": m.is_read is not None,
                    "received_at": m.created_at.isoformat(),
                    "related_resource": {
                        "type": m.related_resource_type,
                        "id": str(m.related_resource_id) if m.related_resource_id else None
                    } if m.related_resource_type else None,
                }
                for m in messages
            ]
        }
    except Exception as e:
        logger.error(f"Error fetching inbox: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch messages")


@router.get("/messages/unread/count")
def get_unread_count(
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Get unread message count.
    
    **Roles**: any admin.
    """
    try:
        from app.services.admin_messaging import get_unread_count
        
        count = get_unread_count(current_user.id, db)
        
        return {
            "status": "success",
            "unread_count": count
        }
    except Exception as e:
        logger.error(f"Error getting unread count: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get unread count")


@router.get("/messages/sent")
def get_sent_messages(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Get sent messages for current admin.
    
    **Roles**: any admin.
    """
    try:
        from app.services.admin_messaging import count_sent, get_sent_messages

        # Count and page in SQL — see the note in get_inbox.
        total = count_sent(current_user.id, db)
        messages = get_sent_messages(
            current_user.id, db=db, limit=size, offset=(page - 1) * size
        )
        
        return {
            "status": "success",
            "total": total,
            "page": page,
            "size": size,
            "messages": [
                {
                    "id": str(m.id),
                    "to": m.receiver.name if m.receiver else None,
                    "receiver_role": m.receiver.role if m.receiver else None,
                    "subject": m.subject,
                    "preview": m.body[:100] + "..." if len(m.body) > 100 else m.body,
                    "message_type": m.message_type,
                    "is_urgent": m.is_urgent,
                    "is_read": m.is_read is not None,
                    "sent_at": m.created_at.isoformat(),
                    "related_resource": {
                        "type": m.related_resource_type,
                        "id": str(m.related_resource_id) if m.related_resource_id else None
                    } if m.related_resource_type else None,
                }
                for m in messages
            ]
        }
    except Exception as e:
        logger.error(f"Error fetching sent messages: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch messages")


@router.get("/messages/{message_id}")
def get_message(
    message_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Get full message details.
    
    **Roles**: any admin.
    """
    try:
        from app.services.admin_messaging import mark_read, AdminMessage

        message = db.query(AdminMessage) \
            .filter(AdminMessage.id == message_id) \
            .first()

        if not message:
            raise HTTPException(status_code=404, detail="Message not found")

        if message.receiver_id != current_user.id and message.sender_id != current_user.id:
            raise HTTPException(status_code=403, detail="Permission denied")

        if message.receiver_id == current_user.id and not message.is_read:
            mark_read(message_id, db)
            message.is_read = now_utc()

        return {
            "status": "success",
            "id": str(message.id),
            "from": {
                "id": str(message.sender_id),
                "name": message.sender.name if message.sender else None,
                "role": message.sender.role if message.sender else None,
            },
            "to": {
                "id": str(message.receiver_id),
                "name": message.receiver.name if message.receiver else None,
                "role": message.receiver.role if message.receiver else None,
            },
            "subject": message.subject,
            "body": message.body,
            "message_type": message.message_type,
            "is_urgent": message.is_urgent,
            "is_read": message.is_read is not None,
            "created_at": message.created_at.isoformat(),
            "related_resource": {
                "type": message.related_resource_type,
                "id": str(message.related_resource_id) if message.related_resource_id else None
            } if message.related_resource_type else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching message {message_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch message")


# ── Announcements ─────────────────────────────────────────────────────────────

class CreateAnnouncementRequest(BaseModel):
    """Request body for creating an announcement."""
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=5000)
    scope: str = Field(..., description="ward | taluka | district | state")
    ward_id: Optional[uuid.UUID] = Field(None, description="Required for scope=ward")
    taluka_id: Optional[uuid.UUID] = Field(None, description="Required for scope=taluka")
    district_id: Optional[uuid.UUID] = Field(None, description="Required for scope=district")
    expires_at: Optional[datetime] = Field(None, description="Optional expiry datetime (UTC)")
    location_lat: Optional[float] = Field(None, description="Latitude of a location citizens can view on map")
    location_lng: Optional[float] = Field(None, description="Longitude of a location citizens can view on map")


@router.post("/announcements", status_code=status.HTTP_201_CREATED)
def create_announcement(
    body: CreateAnnouncementRequest,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Broadcast an announcement to citizens.

    Scope rules:
    - ``ward``     → citizens in a specific ward
    - ``taluka``   → all citizens in a taluka
    - ``district`` → all citizens in a district
    - ``state``    → everyone (super-admin only)

    Push notifications are queued, not sent inline: the `jobs` service picks
    the announcement up on its next cycle and fans out to matching citizens.
    Sending here meant one commit and one blocking FCM call per recipient
    inside this handler, and a delivery failure was swallowed behind a 201.

    **Roles**: any admin.
    """
    valid_scopes = {"ward", "taluka", "district", "state"}
    if body.scope not in valid_scopes:
        raise HTTPException(status_code=400, detail=f"scope must be one of: {', '.join(valid_scopes)}")
    if body.scope == "state" and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Only super-admin can broadcast state-wide announcements")

    try:
        ann = Announcement(
            id=uuid.uuid4(),
            title=body.title,
            body=body.body,
            author_id=current_user.id,
            scope=body.scope,
            ward_id=body.ward_id,
            taluka_id=body.taluka_id,
            district_id=body.district_id,
            expires_at=body.expires_at,
            location_lat=body.location_lat,
            location_lng=body.location_lng,
        )
        db.add(ann)
        db.commit()
        db.refresh(ann)

        # Push notifications to matching citizens
        _queue_announcement_push(ann)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating announcement: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create announcement.")

    logger.info(f"Admin {current_user.id} created announcement '{body.title}' scope={body.scope}")
    return _announcement_out(ann)


@router.get("/announcements")
def list_announcements(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    active_only: bool = Query(True, description="Exclude expired announcements"),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """List announcements in the admin's scope. **Roles**: any admin."""
    try:
        query = db.query(Announcement)
        if current_user.role == "ward_admin" and current_user.ward_id:
            query = query.filter(
                (Announcement.ward_id == current_user.ward_id) |
                (Announcement.taluka_id == current_user.taluka_id) |
                (Announcement.district_id == current_user.district_id) |
                (Announcement.scope == "state")
            )
        elif current_user.role == "taluka_admin" and current_user.taluka_id:
            query = query.filter(
                (Announcement.taluka_id == current_user.taluka_id) |
                (Announcement.district_id == current_user.district_id) |
                (Announcement.scope == "state")
            )
        elif current_user.role == "district_admin" and current_user.district_id:
            query = query.filter(
                (Announcement.district_id == current_user.district_id) |
                (Announcement.scope == "state")
            )

        if active_only:
            query = query.filter(
                (Announcement.expires_at.is_(None)) | (Announcement.expires_at > now_utc())
            )

        total = query.count()
        items = query.order_by(Announcement.created_at.desc()).offset((page - 1) * size).limit(size).all()
    except Exception as e:
        logger.error(f"Error listing announcements: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch announcements.")

    return {"items": [_announcement_out(a) for a in items], "total": total, "page": page, "size": size}


class UpdateAnnouncementRequest(BaseModel):
    """Request body for editing an announcement. All fields optional.

    ``scope`` and its three target ids are deliberately absent. Re-scoping a
    delivered announcement is not an edit — the citizens who received it are not
    the citizens who would receive it now — so that is a delete plus a new post,
    which is also what keeps the push audit honest.
    """

    title: Optional[str] = Field(None, min_length=1, max_length=200)
    body: Optional[str] = Field(None, min_length=1, max_length=5000)
    expires_at: Optional[datetime] = Field(None, description="New expiry datetime (UTC)")
    location_lat: Optional[float] = Field(None, description="Latitude citizens can view on map")
    location_lng: Optional[float] = Field(None, description="Longitude citizens can view on map")


@router.patch("/announcements/{announcement_id}")
def update_announcement(
    announcement_id: uuid.UUID,
    body: UpdateAnnouncementRequest,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Edit an announcement's text, expiry or map pin.

    Authorization matches ``DELETE``: author, or super-admin.

    **Editing never re-notifies.** ``push_dispatched_at`` is the latch the
    ``jobs`` service reads to decide who still needs a push; clearing it here
    would re-deliver the announcement to every matching citizen, so a typo fix
    would buzz an entire district a second time. It is therefore left untouched,
    and returned in the response so the console can say plainly that the edit
    will not reach anyone who already received the original.

    Re-scoping is not offered — see ``UpdateAnnouncementRequest``.

    **Roles**: any admin (must be author or super-admin).
    """
    ann = db.query(Announcement).filter(Announcement.id == announcement_id).first()
    if not ann:
        raise HTTPException(status_code=404, detail="Announcement not found")
    if str(ann.author_id) != str(current_user.id) and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="You can only edit your own announcements")

    # `exclude_unset` rather than `exclude_none`: an explicit null is how a
    # caller clears an expiry or removes a map pin, and treating that the same
    # as "field omitted" would make those two fields impossible to unset.
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        for field, value in changes.items():
            setattr(ann, field, value)
        db.commit()
        db.refresh(ann)
    except Exception as e:
        logger.error(f"Error updating announcement {announcement_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update announcement.")

    logger.info(
        f"Admin {current_user.id} edited announcement {announcement_id} "
        f"fields={sorted(changes)} already_dispatched={ann.push_dispatched_at is not None}"
    )
    return _announcement_out(ann)


@router.delete("/announcements/{announcement_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_announcement(
    announcement_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Delete an announcement. **Roles**: any admin (must be author or super-admin)."""
    ann = db.query(Announcement).filter(Announcement.id == announcement_id).first()
    if not ann:
        raise HTTPException(status_code=404, detail="Announcement not found")
    if str(ann.author_id) != str(current_user.id) and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="You can only delete your own announcements")
    db.delete(ann)
    db.commit()
    logger.info(f"Admin {current_user.id} deleted announcement {announcement_id}")


# ── Flags / moderation ────────────────────────────────────────────────────────

@router.get("/flags")
def list_flags(
    status_filter: Optional[str] = Query(None, alias="status", description="pending|reviewed|dismissed"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """List content flags/reports for moderation. **Roles**: any admin (scoped).

    Scoped to the admin's jurisdiction. This returned every flag in the system
    to any of the four admin roles, so a single-ward moderator saw — and could
    act on — reports from every ward in the state.
    """
    try:
        query = db.query(IssueFlag)
        allowed = scoped_issue_ids(current_user, db)
        if allowed is not None:
            query = query.filter(IssueFlag.issue_id.in_(allowed))
        if status_filter:
            query = query.filter(IssueFlag.status == status_filter)
        total = query.count()
        flags = query.order_by(IssueFlag.created_at.desc()).offset((page - 1) * size).limit(size).all()
    except Exception as e:
        logger.error(f"Error listing flags: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch flags.")
    return {
        "items": [_flag_out(f) for f in flags],
        "total": total, "page": page, "size": size,
    }


@router.patch("/flags/{flag_id}")
def update_flag(
    flag_id: uuid.UUID,
    flag_status: str = Query(..., alias="status", description="reviewed|dismissed"),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Mark a flag as reviewed or dismissed. **Roles**: any admin."""
    if flag_status not in ("reviewed", "dismissed"):
        raise HTTPException(status_code=400, detail="status must be 'reviewed' or 'dismissed'")
    query = db.query(IssueFlag).filter(IssueFlag.id == flag_id)
    allowed = scoped_issue_ids(current_user, db)
    if allowed is not None:
        query = query.filter(IssueFlag.issue_id.in_(allowed))
    flag = query.first()
    if not flag:
        # 404 rather than 403 — an out-of-jurisdiction flag should not be
        # distinguishable from one that does not exist.
        raise HTTPException(status_code=404, detail="Flag not found")
    flag.status = flag_status
    db.commit()
    logger.info(f"Admin {current_user.id} set flag {flag_id} → {flag_status}")
    return _flag_out(flag)


# ── Heatmap ───────────────────────────────────────────────────────────────────

@router.get("/heatmap", response_model=List[Dict])
def get_heatmap(
    issue_type: Optional[str] = Query(None),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Heatmap data for admin map visualization. **Roles**: any admin."""
    try:
        query = apply_admin_scope(db.query(Issue), current_user, Issue)
        query = query.filter(Issue.status.in_(["open", "assigned", "in_progress"]))
        query = apply_not_deleted_filter(query)
        if issue_type:
            query = query.filter(Issue.issue_type == issue_type)
        weight_map = {"high": 3, "medium": 2, "low": 1}
        result = [
            {
                "lat": i.latitude, "lng": i.longitude,
                "weight": weight_map.get(i.severity, 1),
                "issue_type": i.issue_type, "id": str(i.id),
                "is_sos": i.is_sos, "address": i.address, "ward_name": i.ward,
                "status": i.status, "description": i.description,
            }
            for i in query.all()
        ]
    except Exception as e:
        logger.error(f"Error fetching heatmap: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch heatmap data.")
    return result


# ── Heatmap Time Machine ─────────────────────────────────────────────────────

@router.get("/heatmap/timemachine", response_model=List[Dict])
def heatmap_time_machine(
    start_date: str = Query(..., description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(..., description="End date (YYYY-MM-DD)"),
    # ge=1: with no lower bound, interval_days=0 made `current += timedelta(days=0)`
    # loop forever appending a snapshot dict each pass, until the process was
    # OOM-killed. One GET from any admin role took the pod down.
    interval_days: int = Query(1, ge=1, le=365, description="Interval in days for each snapshot"),
    issue_type: Optional[str] = Query(None),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Heatmap Time Machine — Returns snapshots of issue state over a date range.

    Allows admins to visualize how issues appeared and were resolved over time
    (e.g., watching a cluster of potholes disappear over a 2-week period).

    **Roles**: any admin.

    Returns:
        List of snapshots, each containing date and active issue positions.
    """
    try:
        from datetime import datetime as dt

        start = dt.strptime(start_date, "%Y-%m-%d")
        end = dt.strptime(end_date, "%Y-%m-%d")

        if (end - start).days > 90:
            raise HTTPException(status_code=400, detail="Maximum range is 90 days.")

        # Fetch all issues created in the range
        issues = (
            apply_admin_scope(db.query(Issue), current_user, Issue)
            .filter(
                Issue.created_at <= end + timedelta(days=1),
            )
        )
        issues = apply_not_deleted_filter(issues)
        if issue_type:
            issues = issues.filter(Issue.issue_type == issue_type)
        all_issues = issues.all()

        snapshots = []
        current = start
        weight_map = {"high": 3, "medium": 2, "low": 1}

        while current <= end:
            # For each snapshot date, find issues that were active (not yet resolved)
            active = []
            for i in all_issues:
                created = i.created_at.replace(tzinfo=None) if i.created_at else None
                resolved = i.resolved_at.replace(tzinfo=None) if i.resolved_at else None

                if created and created <= current:
                    if resolved is None or resolved > current:
                        active.append({
                            "lat": i.latitude,
                            "lng": i.longitude,
                            "weight": weight_map.get(i.severity, 1),
                            "issue_type": i.issue_type,
                            "id": str(i.id),
                            "status": i.status,
                        })

            snapshots.append({
                "date": current.strftime("%Y-%m-%d"),
                "active_count": len(active),
                "points": active,
            })
            current += timedelta(days=interval_days)

        logger.info(f"Heatmap time machine: {len(snapshots)} snapshots from {start_date} to {end_date}")
        return snapshots
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Heatmap time machine error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to generate heatmap timeline.")


# ── SOS Active Issues ─────────────────────────────────────────────────────────

@router.get("/sos/active", response_model=List[Dict])
def get_active_sos(
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Get all active SOS / Emergency issues for the red alert banner.

    **Roles**: any admin.

    Returns:
        List of active SOS issues with location, type, and time since report.
    """
    try:
        sos_issues = (
            apply_admin_scope(db.query(Issue), current_user, Issue)
            .filter(
                Issue.is_sos == True,
                Issue.status.in_(["open", "assigned", "in_progress"]),
            )
            .order_by(Issue.created_at.desc())
        )
        sos_issues = apply_not_deleted_filter(sos_issues).all()

        now = now_utc()
        result = []
        for i in sos_issues:
            elapsed = now - i.created_at.replace(tzinfo=None)
            result.append({
                "id": str(i.id),
                "id_short": str(i.id)[:8],
                "issue_type": i.issue_type,
                "status": i.status,
                "address": i.address,
                "ward": i.ward,
                "latitude": i.latitude,
                "longitude": i.longitude,
                "created_at": i.created_at.isoformat(),
                "minutes_ago": round(elapsed.total_seconds() / 60),
            })

        logger.debug(f"Active SOS issues: {len(result)}")
        return result
    except Exception as e:
        logger.error(f"Error fetching active SOS: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch SOS issues.")


# ── Squads (Multi-Worker) ────────────────────────────────────────────────────

@router.post("/squads", response_model=Dict, status_code=status.HTTP_201_CREATED)
def create_squad(
    issue_id: uuid.UUID = Query(..., description="Issue UUID to create squad for"),
    lead_worker_id: uuid.UUID = Query(..., description="Lead worker UUID"),
    assistant_ids: str = Query("", description="Comma-separated assistant worker UUIDs"),
    notes: Optional[str] = Query(None, description="Squad notes"),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Create a multi-worker squad for a large issue.

    Assigns a lead worker and optional assistants to collaboratively resolve
    complex issues like fallen trees or major pipe bursts.

    **Roles**: any admin.
    """
    try:
        from app.models.issue_squad import IssueSquad

        # Scoped: this assigned a lead worker to any issue in the country for
        # any of the four admin roles.
        issue = db.query(Issue).filter(Issue.id == issue_id)
        issue = apply_admin_scope(issue, current_user, Issue)
        issue = apply_not_deleted_filter(issue).first()
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")

        # Parse assistant IDs
        parsed_assistants = []
        if assistant_ids.strip():
            parsed_assistants = [a.strip() for a in assistant_ids.split(",") if a.strip()]

        # Verify lead worker exists
        lead = db.query(User).filter(User.id == lead_worker_id, User.role == "worker").first()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead worker not found")

        # Check no existing squad
        existing = db.query(IssueSquad).filter(IssueSquad.issue_id == issue_id).first()
        if existing:
            raise HTTPException(status_code=409, detail="Squad already exists for this issue")

        squad = IssueSquad(
            issue_id=issue_id,
            lead_worker_id=lead_worker_id,
            assistant_ids=parsed_assistants,
            notes=notes,
        )
        db.add(squad)

        # Also assign lead worker to the issue
        issue.assigned_worker_id = lead_worker_id
        if issue.status == "open":
            issue.status = "assigned"

        db.commit()
        db.refresh(squad)

        # Notify all squad members
        from app.services.notification_service import notify
        all_worker_ids = [str(lead_worker_id)] + parsed_assistants
        for wid in all_worker_ids:
            worker = db.query(User).filter(User.id == wid).first()
            if worker:
                notify(
                    db=db, user_id=wid,
                    title="Squad Assignment",
                    body=f"You've been assigned to a squad for {issue.issue_type} issue in {issue.ward or 'your area'}.",
                    notification_type="assignment",
                    issue_id=str(issue.id),
                    fcm_token=worker.fcm_token,
                )

        logger.info(f"Squad created for issue {issue_id}: lead={lead_worker_id}, assistants={parsed_assistants}")

        return {
            "id": str(squad.id),
            "issue_id": str(squad.issue_id),
            "lead_worker_id": str(squad.lead_worker_id),
            "assistant_ids": squad.assistant_ids,
            "status": squad.status,
            "notes": squad.notes,
            "created_at": squad.created_at.isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating squad: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create squad.")


@router.get("/squads/{issue_id}", response_model=Dict)
def get_squad(
    issue_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Get squad details for an issue. **Roles**: any admin."""
    try:
        from app.models.issue_squad import IssueSquad

        squad = db.query(IssueSquad).filter(IssueSquad.issue_id == issue_id).first()
        if not squad:
            raise HTTPException(status_code=404, detail="No squad found for this issue")

        # Resolve worker names
        lead = db.query(User).filter(User.id == squad.lead_worker_id).first()
        assistants = []
        for aid in squad.assistant_ids:
            w = db.query(User).filter(User.id == aid).first()
            if w:
                assistants.append({"id": str(w.id), "name": w.name, "phone": w.phone})

        return {
            "id": str(squad.id),
            "issue_id": str(squad.issue_id),
            "lead_worker": {"id": str(lead.id), "name": lead.name, "phone": lead.phone} if lead else None,
            "assistants": assistants,
            "status": squad.status,
            "notes": squad.notes,
            "created_at": squad.created_at.isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching squad: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch squad.")


# ── Geofences ─────────────────────────────────────────────────────────────────


def _jurisdiction_chain(
    db: Session,
    ward_id: Optional[uuid.UUID],
    taluka_id: Optional[uuid.UUID],
    district_id: Optional[uuid.UUID],
    *,
    strict: bool,
) -> dict:
    """Expand the most specific id given into a full ward/taluka/district chain.

    ``apply_admin_scope`` filters on whichever single column matches the
    caller's tier, so a zone stored with only ``ward_id`` would be invisible to
    that ward's own taluka_admin. Every level therefore has to be present.

    The chain is read from the ``Ward``/``Taluka`` rows rather than copied from
    whoever supplied it. The denormalised columns on ``User`` are not reliably
    complete — a ward_admin can have ``ward_id`` set and ``taluka_id`` NULL —
    and trusting them produced exactly the under-populated row described above.

    Args:
        strict: raise 400 on an id that resolves to nothing. Used for
            caller-supplied ids; a stale id on an admin's own record should
            narrow their reach, not fail their request.
    """
    def _missing(what: str):
        if strict:
            raise HTTPException(status_code=400, detail=f"{what} does not exist")
        return None

    if ward_id:
        ward = db.query(Ward).filter(Ward.id == ward_id).first()
        if not ward:
            _missing("ward_id")
        else:
            taluka = db.query(Taluka).filter(Taluka.id == ward.taluka_id).first()
            return {
                "ward_id": ward.id,
                "taluka_id": ward.taluka_id,
                "district_id": taluka.district_id if taluka else None,
            }

    if taluka_id:
        taluka = db.query(Taluka).filter(Taluka.id == taluka_id).first()
        if not taluka:
            _missing("taluka_id")
        else:
            return {
                "ward_id": None,
                "taluka_id": taluka.id,
                "district_id": taluka.district_id,
            }

    return {"ward_id": None, "taluka_id": None, "district_id": district_id}


def _resolve_geofence_scope(current_user: User, body, db: Session) -> dict:
    """Decide which jurisdiction a new geofence belongs to.

    A **scoped admin** always gets their own scope, regardless of what the
    request body asked for. Honouring a caller-supplied jurisdiction here would
    be the same self-widening hole that ``PUT /auth/profile`` had: an admin able
    to nominate a scope can create — and therefore manage — a zone anywhere.

    A **super-admin** may target a jurisdiction explicitly. Supplying nothing
    yields all-NULL: a state-level zone.

    Returns:
        A dict of ``ward_id``/``taluka_id``/``district_id`` ready to splat onto
        the model.

    Raises:
        400: A super-admin named a ward or taluka that does not exist.
    """
    if current_user.role != "admin":
        # A scoped admin with no scope at all is already refused upstream by
        # get_admin_scope_filter's DENY_ALL, so this cannot quietly produce a
        # state-level zone.
        return _jurisdiction_chain(
            db,
            current_user.ward_id,
            current_user.taluka_id,
            current_user.district_id,
            strict=False,
        )

    return _jurisdiction_chain(
        db, body.ward_id, body.taluka_id, body.district_id, strict=True
    )


def _geofence_out(gf: Geofence, created_by_name: Optional[str] = None) -> GeofenceResponse:
    """One serializer, so the four endpoints cannot drift apart."""
    return GeofenceResponse(
        id=gf.id,
        name=gf.name,
        latitude=gf.latitude,
        longitude=gf.longitude,
        radius_km=gf.radius_km,
        created_by_name=(
            created_by_name if created_by_name is not None
            else (gf.created_by.name if gf.created_by else None)
        ),
        created_at=gf.created_at,
        ward_id=gf.ward_id,
        taluka_id=gf.taluka_id,
        district_id=gf.district_id,
    )


def _geofence_in_scope(geofence_id: uuid.UUID, current_user: User, db: Session) -> Geofence:
    """Fetch a geofence the caller is allowed to act on, or 404.

    404 rather than 403 on purpose: a zone outside your jurisdiction should not
    be distinguishable from one that does not exist, or the endpoint becomes a
    way to enumerate other districts' zones.
    """
    geofence = (
        apply_admin_scope(db.query(Geofence), current_user, Geofence)
        .filter(Geofence.id == geofence_id)
        .first()
    )
    if not geofence:
        raise HTTPException(
            status_code=404, detail="Geofence not found or outside your jurisdiction"
        )
    return geofence


@router.get("/geofences", response_model=GeofenceListResponse)
def list_geofences(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """List geofences in the admin's jurisdiction, paginated.

    - super-admin:    every zone, including state-level ones
    - district_admin: zones in their district
    - taluka_admin:   zones in their taluka
    - ward_admin:     zones in their ward

    Zones with no jurisdiction (all three ids NULL) are state-level and appear
    only for a super-admin. Every geofence created before ``a9b0c1d2e3`` is in
    that category — there was no boundary geometry to derive a jurisdiction
    from, so they were left for a super-admin to assign rather than guessed at.

    **Roles**: any admin.

    Args:
        page: Page number (1-indexed), default 1
        size: Items per page (1-200), default 20

    Returns:
        GeofenceListResponse with items, total, page, size
    """
    try:
        query = apply_admin_scope(db.query(Geofence), current_user, Geofence)

        total = query.count()

        geofences = (
            query
            .order_by(Geofence.created_at.desc(), Geofence.id.desc())
            .offset((page - 1) * size)
            .limit(size)
            .all()
        )

        return GeofenceListResponse(
            items=[_geofence_out(gf) for gf in geofences],
            total=total,
            page=page,
            size=size,
        )
    except Exception as e:
        logger.error(f"Error listing geofences: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch geofences.")


@router.post("/geofences", response_model=GeofenceResponse, status_code=status.HTTP_201_CREATED)
def create_geofence(
    body: CreateGeofenceRequest,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Create a new geofence zone.

    The zone is stamped with the caller's own jurisdiction. A scoped admin
    cannot choose one — supplying ``ward_id``/``taluka_id``/``district_id`` is
    honoured only for a super-admin, because an admin able to nominate a scope
    could create, and thereafter manage, a zone anywhere in the state.

    **Roles**: any admin.

    Args:
        body: CreateGeofenceRequest with name, latitude, longitude, radius_km,
              and optionally a jurisdiction (super-admin only).

    Returns:
        Created GeofenceResponse
    """
    try:
        # Validate input
        if not body.name or not body.name.strip():
            raise HTTPException(status_code=400, detail="Zone name is required")
        
        if body.latitude < -90 or body.latitude > 90:
            raise HTTPException(status_code=400, detail="Latitude must be between -90 and 90")
        
        if body.longitude < -180 or body.longitude > 180:
            raise HTTPException(status_code=400, detail="Longitude must be between -180 and 180")
        
        if body.radius_km <= 0:
            raise HTTPException(status_code=400, detail="Radius must be greater than 0")
        
        # FIX: HIGH PRIORITY BUG #12 - Geofence validation incomplete
        # Validate geofence radius is within reasonable bounds
        try:
            validate_geofence_radius(body.radius_km)
            validate_geofence_area(body.radius_km)
        except ValueError as e:
            # ValueError from the geofence validators — those messages are
            # written for operators and are safe, but bound the length.
            raise HTTPException(status_code=400, detail=str(e)[:200])
        
        # Where this zone belongs. Resolved before the overlap check so the
        # comparison is against zones the caller can actually see.
        scope = _resolve_geofence_scope(current_user, body, db)

        # MEDIUM PRIORITY BUG FIX #4: Check for overlapping geofences
        #
        # Scoped to the caller's jurisdiction. Unscoped, this both loaded every
        # zone in the state on each create and named the colliding ones in the
        # 409 — so a ward_admin learned the names of zones in districts they
        # cannot otherwise see. The trade-off is that overlaps across a
        # jurisdiction boundary are no longer reported, which is the right way
        # round: a neighbouring district's zone is not this admin's to move.
        existing_geofences = apply_admin_scope(
            db.query(Geofence), current_user, Geofence
        ).all()
        overlapping = []
        for existing in existing_geofences:
            if check_geofences_intersect(
                body.latitude, body.longitude, body.radius_km,
                existing.latitude, existing.longitude, existing.radius_km
            ):
                overlapping.append(existing.name)
        
        if overlapping:
            raise HTTPException(
                status_code=409,
                detail=f"Geofence overlaps with existing zones: {', '.join(overlapping)}. "
                       f"Please adjust the location or radius to avoid overlap with: {', '.join(overlapping[:3])}"
                       + (" and more" if len(overlapping) > 3 else "")
            )
        
        # Create geofence
        geofence = Geofence(
            id=uuid.uuid4(),
            name=body.name.strip(),
            latitude=body.latitude,
            longitude=body.longitude,
            radius_km=body.radius_km,
            created_by_id=current_user.id,
            **scope,
        )

        db.add(geofence)
        db.commit()
        db.refresh(geofence)

        logger.info(
            f"Admin {current_user.id} created geofence {geofence.id}: {geofence.name} "
            f"ward={scope['ward_id']} taluka={scope['taluka_id']} district={scope['district_id']}"
        )

        return _geofence_out(geofence, created_by_name=current_user.name)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error creating geofence: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create geofence.")


@router.patch("/geofences/{geofence_id}", response_model=GeofenceResponse)
def update_geofence(
    geofence_id: uuid.UUID,
    body: UpdateGeofenceRequest,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Update a geofence zone (all fields optional).

    Restricted to zones inside the caller's jurisdiction. Until this check
    existed, any admin tier could edit any zone in the state.

    A zone's jurisdiction is not editable here — moving a zone between wards is
    a transfer of ownership, not a field update, and re-scoping it out from
    under its current admin should be a deliberate super-admin act rather than a
    side effect of nudging a radius.

    **Roles**: any admin, within their own jurisdiction.

    Args:
        geofence_id: UUID of the geofence to update
        body: UpdateGeofenceRequest with optional fields

    Returns:
        Updated GeofenceResponse
    """
    try:
        geofence = _geofence_in_scope(geofence_id, current_user, db)

        # Update fields if provided
        if body.name is not None:
            if not body.name or not body.name.strip():
                raise HTTPException(status_code=400, detail="Zone name cannot be empty")
            geofence.name = body.name.strip()
        
        if body.latitude is not None:
            if body.latitude < -90 or body.latitude > 90:
                raise HTTPException(status_code=400, detail="Latitude must be between -90 and 90")
            geofence.latitude = body.latitude
        
        if body.longitude is not None:
            if body.longitude < -180 or body.longitude > 180:
                raise HTTPException(status_code=400, detail="Longitude must be between -180 and 180")
            geofence.longitude = body.longitude
        
        if body.radius_km is not None:
            if body.radius_km <= 0:
                raise HTTPException(status_code=400, detail="Radius must be greater than 0")
            geofence.radius_km = body.radius_km
        
        db.commit()
        db.refresh(geofence)

        logger.info(f"Admin {current_user.id} updated geofence {geofence.id}")

        return _geofence_out(geofence)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating geofence {geofence_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update geofence.")


@router.get("/geofences/{geofence_id}/workers", response_model=dict)
def geofence_workers(
    geofence_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Which workers are currently inside this zone.

    ``User.latitude``/``longitude`` is a **single mutable point**, not a track —
    it is overwritten on each location update and there is no history table. So
    this answers "where was each worker when they last reported", which is not
    the same as "where are they now".

    That distinction matters enough to be in the payload rather than only in
    this docstring: ``location_updated_at`` and ``location_age_minutes`` are
    returned per worker, and ``stale`` marks anyone whose fix predates the
    threshold below. Without it, a worker whose phone died three hours ago in
    the zone reads as present.

    Workers are restricted to the caller's jurisdiction by the same
    ``user_scope_filter`` used elsewhere, so this cannot be used to locate
    another district's staff.

    **Roles**: any admin, for zones within their own jurisdiction.
    """
    #: A fix older than this is reported but flagged. Half an hour is roughly
    #: the point past which "last seen here" stops implying "is here".
    STALE_AFTER_MINUTES = 30

    geofence = _geofence_in_scope(geofence_id, current_user, db)

    try:
        # Bounding box first so the exact haversine check below runs over a
        # small candidate set rather than every worker in the state.
        lat_delta = geofence.radius_km / 111.0
        lon_delta = geofence.radius_km / max(
            111.0 * abs(math.cos(math.radians(geofence.latitude))), 1e-6
        )

        query = db.query(User).filter(
            User.role == "worker",
            User.is_active == True,  # noqa: E712
            User.latitude.isnot(None),
            User.longitude.isnot(None),
            User.latitude.between(geofence.latitude - lat_delta, geofence.latitude + lat_delta),
            User.longitude.between(geofence.longitude - lon_delta, geofence.longitude + lon_delta),
        )
        scope_filters = user_scope_filter(current_user)
        if scope_filters:
            query = query.filter(*scope_filters)

        now = now_utc()
        workers = []
        for worker in query.limit(500).all():
            if not point_in_geofence(
                worker.latitude, worker.longitude,
                geofence.latitude, geofence.longitude, geofence.radius_km,
            ):
                continue

            age_minutes = None
            if worker.location_updated_at:
                age_minutes = round(
                    (now - worker.location_updated_at).total_seconds() / 60, 1
                )

            workers.append({
                "id": str(worker.id),
                "name": worker.name,
                "is_online": bool(worker.is_online),
                "latitude": worker.latitude,
                "longitude": worker.longitude,
                "distance_km": round(
                    haversine_distance(
                        worker.latitude, worker.longitude,
                        geofence.latitude, geofence.longitude,
                    ),
                    2,
                ),
                "location_updated_at": (
                    worker.location_updated_at.isoformat()
                    if worker.location_updated_at else None
                ),
                "location_age_minutes": age_minutes,
                # No fix timestamp at all is treated as stale — an unknown age
                # is not evidence of freshness.
                "stale": age_minutes is None or age_minutes > STALE_AFTER_MINUTES,
            })

        workers.sort(key=lambda w: w["distance_km"])
    except Exception as e:
        logger.error(f"Error listing workers in geofence {geofence_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch workers in zone.")

    return {
        "geofence": {
            "id": str(geofence.id),
            "name": geofence.name,
            "latitude": geofence.latitude,
            "longitude": geofence.longitude,
            "radius_km": geofence.radius_km,
        },
        "total": len(workers),
        "fresh": sum(1 for w in workers if not w["stale"]),
        "stale_after_minutes": STALE_AFTER_MINUTES,
        "workers": workers,
    }


@router.get("/geofences/{geofence_id}/alerts", response_model=dict)
def geofence_alerts(
    geofence_id: uuid.UUID,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Broadcast history for a zone, newest first.

    History starts at migration ``b0c1d2e3f4``. Before it, ``POST
    /admin/notifications/geofence`` persisted nothing at all, so there is no
    earlier data and none was invented — an empty list on an old zone means "not
    recorded", not "never alerted".

    **Roles**: any admin, for zones within their own jurisdiction.
    """
    geofence = _geofence_in_scope(geofence_id, current_user, db)

    try:
        query = db.query(GeofenceAlert).filter(GeofenceAlert.geofence_id == geofence.id)
        total = query.count()
        rows = (
            query.order_by(GeofenceAlert.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
            .all()
        )
    except Exception as e:
        logger.error(f"Error listing alerts for geofence {geofence_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch zone alerts.")

    return {
        "items": [
            {
                "id": str(row.id),
                "title": row.title,
                "body": row.body,
                "latitude": row.latitude,
                "longitude": row.longitude,
                "radius_km": row.radius_km,
                "recipients_notified": row.recipients_notified,
                "recipients_failed": row.recipients_failed,
                "sent_by_id": str(row.sent_by_id) if row.sent_by_id else None,
                "sent_by_name": row.sent_by.name if row.sent_by else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "size": size,
        "history_since": "migration b0c1d2e3f4 — earlier broadcasts were not recorded",
    }


@router.delete("/geofences/{geofence_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_geofence(
    geofence_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Delete a geofence zone.

    Restricted to zones inside the caller's jurisdiction. This is the endpoint
    the missing check mattered most on: a single-ward admin could delete any
    zone in the state, and the deletion is not recoverable.

    **Roles**: any admin, within their own jurisdiction.

    Args:
        geofence_id: UUID of the geofence to delete
    """
    try:
        geofence = _geofence_in_scope(geofence_id, current_user, db)

        db.delete(geofence)
        db.commit()
        
        logger.info(f"Admin {current_user.id} deleted geofence {geofence.id}")
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting geofence {geofence_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete geofence.")


# ── Analytics ─────────────────────────────────────────────────────────────────

@router.get("/analytics", response_model=Dict)
def get_analytics(
    days: int = Query(30, ge=7, le=90),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Trend data for dashboard charts (scoped). **Roles**: any admin."""
    try:
        since = now_utc() - timedelta(days=days)
        issues = (
            apply_admin_scope(db.query(Issue), current_user, Issue)
            .filter(Issue.created_at >= since)
        )
        issues = apply_not_deleted_filter(issues).all()

        daily: Dict[str, int] = defaultdict(int)
        by_type: Dict[str, int] = defaultdict(int)
        by_status: Dict[str, int] = defaultdict(int)
        by_priority: Dict[str, int] = defaultdict(int)
        ward_counts: Dict[str, int] = defaultdict(int)

        for i in issues:
            daily[i.created_at.strftime("%Y-%m-%d")] += 1
            by_type[i.issue_type] += 1
            by_status[i.status] += 1
            if i.priority:
                by_priority[i.priority] += 1
            if i.ward:
                ward_counts[i.ward] += 1

        top_wards = sorted(ward_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    except Exception as e:
        logger.error(f"Error fetching analytics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch analytics data.")

    return {
        "period_days": days,
        "total_in_period": len(issues),
        "daily_counts": dict(sorted(daily.items())),
        "by_type": dict(by_type),
        "by_status": dict(by_status),
        "by_priority": dict(by_priority),
        "top_wards": [{"ward": w, "count": c} for w, c in top_wards],
    }


#: Below this, an AI classification is treated as needing a human look.
AI_LOW_CONFIDENCE = 0.7

#: A category must move by at least this fraction, off at least this many
#: issues, before it is called out. Without both guards a category going from
#: 1 issue to 3 is a "200% surge", which is noise dressed as a finding.
_MOVEMENT_THRESHOLD = 0.25
_MOVEMENT_MIN_BASE = 5

#: Narrative cache: {(scope_key, days): (generated_at, text)}. Process-local.
_INSIGHT_NARRATIVE_CACHE: Dict[tuple, tuple] = {}

#: How long a generated narrative stays usable.
#:
#: Not keyed per calendar day: the numbers beside it are recomputed on every
#: request, so a day-old sentence sitting next to a fresh `generated_at` would
#: describe a dataset that no longer exists. Fifteen minutes keeps the cost off
#: the page load while staying close enough to the figures it describes — and
#: `narrative_generated_at` is returned so the console can date it explicitly.
_NARRATIVE_TTL = timedelta(minutes=15)


def _clear_insight_cache() -> None:
    """Drop every cached narrative. Used by tests; harmless in production."""
    _INSIGHT_NARRATIVE_CACHE.clear()


def _scope_cache_key(user: User) -> str:
    """Cache identity for an admin's visible dataset, not their user id.

    Two ward admins in the same ward see identical figures, so they should share
    a cache entry rather than each paying for a generation.
    """
    return f"{user.role}:{user.ward_id}:{user.taluka_id}:{user.district_id}"


@router.get("/insights", response_model=dict)
def get_insights(
    days: int = Query(30, ge=7, le=90, description="Length of the current period"),
    narrative: bool = Query(True, description="Include a written summary if a provider is configured"),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Aggregate insights for the admin's jurisdiction.

    This replaces what the console was doing in the browser: paging through
    ``/admin/issues`` 200 rows at a time up to a 2,000-row ceiling, aggregating
    client-side, and rendering an amber banner admitting the figures were a
    sample rather than a total. Every count below is computed in SQL over the
    whole scoped dataset, so it is a total.

    Three blocks:

    * ``ai_quality`` — how much the classifier is producing, how confident it
      is, and how much it has flagged as poorly resolved. This is the data the
      AI-insights screen displays.
    * ``movement`` — each issue type this period against the one before it.
      Guarded twice, by percentage *and* by absolute base, because a category
      going 1 → 3 is not a 200% surge worth an admin's attention.
    * ``anomalies`` — wards whose share of reports moved sharply.

    ``narrative`` is best-effort. If no provider key is configured, or the call
    fails, the field is ``null`` and every number above is still returned — a
    dashboard must not go blank because a third party is down.

    **Roles**: any admin (scoped).
    """
    try:
        now = now_utc()
        current_start = now - timedelta(days=days)
        previous_start = current_start - timedelta(days=days)

        def scoped():
            return apply_not_deleted_filter(
                apply_admin_scope(db.query(Issue), current_user, Issue)
            )

        # ── AI quality, in SQL rather than 2,000 rows over the wire ──────────
        has_ai = (Issue.ai_confidence.isnot(None)) | (Issue.ai_resolution_quality.isnot(None))
        ai_row = scoped().with_entities(
            func.count(Issue.id).filter(has_ai).label("analysed"),
            func.count(Issue.id)
            .filter(Issue.ai_confidence.isnot(None), Issue.ai_confidence < AI_LOW_CONFIDENCE)
            .label("low_confidence"),
            func.count(Issue.id)
            .filter(Issue.ai_resolution_quality == "poor")
            .label("poor_resolutions"),
            func.avg(Issue.ai_confidence).label("avg_confidence"),
            func.count(Issue.id).label("total"),
        ).one()

        # ── Category movement, period over period ────────────────────────────
        def counts_by_type(start, end):
            rows = (
                scoped()
                .filter(Issue.created_at >= start, Issue.created_at < end)
                .with_entities(Issue.issue_type, func.count(Issue.id))
                .group_by(Issue.issue_type)
                .all()
            )
            return {issue_type: count for issue_type, count in rows}

        current = counts_by_type(current_start, now)
        previous = counts_by_type(previous_start, current_start)

        movement = []
        for issue_type in sorted(set(current) | set(previous)):
            now_count = current.get(issue_type, 0)
            then_count = previous.get(issue_type, 0)
            # A category that did not exist last period has no meaningful
            # percentage; report the delta and leave change_pct null rather
            # than dividing by zero or inventing "infinite growth".
            change_pct = (
                round((now_count - then_count) / then_count * 100, 1) if then_count else None
            )
            movement.append({
                "issue_type": issue_type,
                "current": now_count,
                "previous": then_count,
                "delta": now_count - then_count,
                "change_pct": change_pct,
                "notable": (
                    then_count >= _MOVEMENT_MIN_BASE
                    and change_pct is not None
                    and abs(change_pct) >= _MOVEMENT_THRESHOLD * 100
                ),
            })
        movement.sort(key=lambda m: abs(m["delta"]), reverse=True)

        # ── Ward anomalies, same double guard ────────────────────────────────
        def counts_by_ward(start, end):
            rows = (
                scoped()
                .filter(Issue.created_at >= start, Issue.created_at < end, Issue.ward.isnot(None))
                .with_entities(Issue.ward, func.count(Issue.id))
                .group_by(Issue.ward)
                .all()
            )
            return {ward: count for ward, count in rows}

        ward_now = counts_by_ward(current_start, now)
        ward_then = counts_by_ward(previous_start, current_start)

        anomalies = []
        for ward, now_count in ward_now.items():
            then_count = ward_then.get(ward, 0)
            if then_count < _MOVEMENT_MIN_BASE:
                continue
            change = (now_count - then_count) / then_count
            if abs(change) >= _MOVEMENT_THRESHOLD:
                anomalies.append({
                    "ward": ward,
                    "current": now_count,
                    "previous": then_count,
                    "change_pct": round(change * 100, 1),
                    "direction": "up" if change > 0 else "down",
                })
        anomalies.sort(key=lambda a: abs(a["change_pct"]), reverse=True)
        anomalies = anomalies[:10]

        # ── Resolution throughput ────────────────────────────────────────────
        resolved_now = (
            scoped()
            .filter(Issue.resolved_at >= current_start, Issue.resolved_at < now)
            .count()
        )
        resolved_then = (
            scoped()
            .filter(Issue.resolved_at >= previous_start, Issue.resolved_at < current_start)
            .count()
        )
        opened_now = sum(current.values())

        payload = {
            "period_days": days,
            "period_start": current_start.isoformat(),
            "generated_at": now.isoformat(),
            "ai_quality": {
                "total_issues": ai_row.total or 0,
                "analysed": ai_row.analysed or 0,
                "low_confidence": ai_row.low_confidence or 0,
                "poor_resolutions": ai_row.poor_resolutions or 0,
                "avg_confidence": (
                    round(float(ai_row.avg_confidence), 3) if ai_row.avg_confidence else None
                ),
                "low_confidence_threshold": AI_LOW_CONFIDENCE,
            },
            "throughput": {
                "opened": opened_now,
                "resolved": resolved_now,
                "resolved_previous": resolved_then,
                # >1 means the backlog grew over the period.
                "backlog_ratio": (
                    round(opened_now / resolved_now, 2) if resolved_now else None
                ),
            },
            "movement": movement,
            "anomalies": anomalies,
            "narrative": None,
            "narrative_generated_at": None,
        }
    except Exception as e:
        logger.error(f"Error building insights: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to build insights.")

    if narrative:
        text, generated_at = _insight_narrative(current_user, days, payload)
        payload["narrative"] = text
        payload["narrative_generated_at"] = generated_at.isoformat() if generated_at else None

    return payload


def _insight_narrative(current_user: User, days: int, payload: dict):
    """A written summary of the aggregates, and when it was written.

    Cached per (scope, period) for ``_NARRATIVE_TTL``. Without a cache this is
    an LLM round-trip on every dashboard load — a per-view cost, and a
    third-party dependency sitting in front of numbers that are already computed
    and correct.

    Never raises. A provider outage degrades the dashboard to numbers rather
    than to a 500, which is why the caller drops this into an already-complete
    payload rather than building the payload around it.

    Returns:
        ``(text, generated_at)``, either of which may be ``None``.
    """
    if not settings.GROQ_API_KEY:
        return None, None

    key = (_scope_cache_key(current_user), days)
    now = now_utc()

    cached = _INSIGHT_NARRATIVE_CACHE.get(key)
    if cached and now - cached[0] < _NARRATIVE_TTL:
        return cached[1], cached[0]

    try:
        from app.services.ai_service import summarise_insights

        text = summarise_insights(payload)
    except Exception as e:
        # Warning, not error: every number is present and the dashboard is
        # fully usable without this.
        logger.warning(f"Insight narrative unavailable: {e}")
        # Fall back to the expired entry if there is one — a 15-minute-old
        # sentence, dated as such, beats no sentence at all.
        return (cached[1], cached[0]) if cached else (None, None)

    if not text:
        return None, None

    # Bounded so a long-running process cannot accumulate an entry per
    # (scope, period) indefinitely.
    if len(_INSIGHT_NARRATIVE_CACHE) > 256:
        _INSIGHT_NARRATIVE_CACHE.clear()
    _INSIGHT_NARRATIVE_CACHE[key] = (now, text)
    return text, now


@router.post("/notifications/geofence", response_model=dict, status_code=status.HTTP_200_OK)
def send_geofence_notification(
    title: str = Query(..., description="Notification title"),
    body: str = Query(..., description="Notification body"),
    geofence_id: Optional[uuid.UUID] = Query(
        None,
        description="Broadcast to a saved zone. Supply this OR latitude/longitude/radius_km.",
    ),
    latitude: Optional[float] = Query(None, description="Center latitude (ad-hoc circle)"),
    longitude: Optional[float] = Query(None, description="Center longitude (ad-hoc circle)"),
    # le: unbounded, radius_km=99999 push-notified every user in the country
    # with an attacker-controlled title and body, from any admin role.
    radius_km: Optional[float] = Query(None, gt=0, le=50, description="Radius in km (max 50)"),
    location_lat: Optional[float] = Query(None, description="Clickable map location latitude (defaults to center)"),
    location_lng: Optional[float] = Query(None, description="Clickable map location longitude (defaults to center)"),
    current_user: User = Depends(require_role("admin", "district_admin", "taluka_admin", "ward_admin")),
    db: Session = Depends(get_db),
):
    """
    Send bulk push notification to all citizens and workers within a geofence.

    Notifies users who have:
    1. Location data (latitude/longitude from app)
    2. FCM token registered
    3. Active account

    **Two ways to aim it:**

    - ``geofence_id`` — broadcast to a saved zone. The centre and radius come
      from the zone, and the resulting record is **linked** to it, which is what
      makes ``GET /admin/geofences/{id}/alerts`` show a history. The zone must
      be within your jurisdiction.
    - ``latitude`` + ``longitude`` + ``radius_km`` — an ad-hoc circle, for a
      one-off that does not correspond to a saved zone. Recorded with the circle
      but no zone link, because attributing it to one would be a guess.

    Either way the dispatch is written to ``geofence_alerts``. It previously
    existed only as this response and a log line, so a broadcast that reached
    nobody looked exactly like one that reached everybody.

    **Returns**: Count of users notified, plus the id of the recorded alert.
    """
    zone = None
    if geofence_id is not None:
        if any(v is not None for v in (latitude, longitude, radius_km)):
            raise HTTPException(
                status_code=400,
                detail="Supply either geofence_id or latitude/longitude/radius_km, not both",
            )
        zone = _geofence_in_scope(geofence_id, current_user, db)
        latitude, longitude, radius_km = zone.latitude, zone.longitude, zone.radius_km
        if radius_km > 50:
            raise HTTPException(
                status_code=400,
                detail=f"Zone radius {radius_km:g} km exceeds the 50 km broadcast limit",
            )
    elif None in (latitude, longitude, radius_km):
        raise HTTPException(
            status_code=400,
            detail="Supply geofence_id, or all of latitude, longitude and radius_km",
        )

    try:
        from app.services.notification_service import notify
        from math import pi, acos, sin, cos

        logger.info(
            f"Geofence notification request: center=({latitude},{longitude}), "
            f"radius={radius_km}km, title='{title}'"
        )

        # Haversine calculation helper
        lat_rad = latitude * pi / 180.0
        lon_rad = longitude * pi / 180.0
        R = 6371  # Earth radius in km

        def haversine_distance(user_lat, user_lon):
            """Calculate distance in km using Haversine formula"""
            if user_lat is None or user_lon is None:
                return float('inf')
            user_lat_rad = user_lat * pi / 180.0
            user_lon_rad = user_lon * pi / 180.0
            
            cos_angle = (
                sin(lat_rad) * sin(user_lat_rad) +
                cos(lat_rad) * cos(user_lat_rad) *
                cos(user_lon_rad - lon_rad)
            )
            cos_angle = max(-1, min(1, cos_angle))
            return R * acos(cos_angle)

        # Candidates with location data and an FCM token, pre-filtered by a
        # bounding box before the exact haversine check below, and capped.
        #
        # This pulled the entire user table into memory — every active citizen
        # and worker in the country — and then filtered in Python. With the
        # radius now bounded at 50 km the box is small, but the cap stays as a
        # backstop: a single request must not be able to fan out without limit.
        lat_delta = radius_km / 111.0
        lon_delta = radius_km / max(111.0 * abs(cos(lat_rad)), 1e-6)

        users = (
            db.query(User)
            .filter(
                User.is_active == True,  # noqa: E712
                User.latitude.isnot(None),
                User.longitude.isnot(None),
                User.fcm_token.isnot(None),
                User.role.in_(["citizen", "worker"]),
                User.latitude.between(latitude - lat_delta, latitude + lat_delta),
                User.longitude.between(longitude - lon_delta, longitude + lon_delta),
            )
            .limit(settings.GEOFENCE_NOTIFY_MAX_RECIPIENTS)
            .all()
        )

        logger.debug(f"Found {len(users)} users with location data and FCM tokens")

        notified_count = 0
        failed_count = 0

        for user in users:
            try:
                distance_km = haversine_distance(user.latitude, user.longitude)
                
                if distance_km <= radius_km:
                    # User is within geofence — save to DB and send FCM push
                    # If no explicit location provided, default to the geofence center
                    pin_lat = location_lat if location_lat is not None else latitude
                    pin_lng = location_lng if location_lng is not None else longitude
                    notify(
                        db=db,
                        user_id=str(user.id),
                        title=title,
                        body=body,
                        notification_type="system",
                        fcm_token=user.fcm_token,
                        location_lat=pin_lat,
                        location_lng=pin_lng,
                    )
                    notified_count += 1
                    logger.debug(
                        f"Notification sent to user {user.id} ({user.role}), distance: {distance_km:.1f}km"
                    )
            except Exception as e:
                failed_count += 1
                logger.warn(f"Failed to notify user {user.id}: {e}")

        logger.info(
            f"Geofence notification complete: {notified_count} users notified, "
            f"{failed_count} failures"
        )

        # Record the broadcast. This used to exist only as the HTTP response and
        # a log line, so a zone's alert history could not be shown, audited, or
        # reconstructed — and a broadcast that reached nobody was
        # indistinguishable from one that reached everybody.
        #
        # `geofence_id` is set only when the caller actually named a zone. For
        # an ad-hoc circle it stays NULL rather than being matched to whichever
        # saved zone happens to sit nearby, which would be a guess. The circle
        # is stored either way, since that is the fact that occurred.
        alert = GeofenceAlert(
            id=uuid.uuid4(),
            geofence_id=zone.id if zone else None,
            latitude=latitude,
            longitude=longitude,
            radius_km=radius_km,
            title=title[:200],
            body=body,
            sent_by_id=current_user.id,
            recipients_notified=notified_count,
            recipients_failed=failed_count,
            ward_id=current_user.ward_id,
            taluka_id=current_user.taluka_id,
            district_id=current_user.district_id,
        )
        db.add(alert)
        db.commit()

        return {
            "success": True,
            "alert_id": str(alert.id),
            "citizens_notified": notified_count,
            "citizens_failed": failed_count,
            "geofence": {
                "center": {"latitude": latitude, "longitude": longitude},
                "radius_km": radius_km,
            },
        }
    except Exception as e:
        logger.error(f"Geofence notification error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to send geofence notifications.")


# ── Private helpers ───────────────────────────────────────────────────────────

def scoped_issue_ids(current_user: User, db: Session):
    """Subquery of the issue IDs this admin may act on.

    Several admin resources — flags, disputes, squads, surveys — are not
    themselves geographic but hang off an issue that is. Filtering them by
    ``<model>.issue_id.in_(scoped_issue_ids(...))`` gives them the same
    jurisdiction boundary as the issue list, in one place.

    Returns ``None`` for a super-admin, meaning "no restriction" — callers
    should skip the filter entirely rather than applying an always-true one.
    """
    if current_user.role == "admin":
        return None
    return apply_admin_scope(db.query(Issue.id), current_user, Issue).scalar_subquery()


def _assert_scope_within_caller(
    caller: User,
    *,
    ward_id: uuid.UUID | None,
    taluka_id: uuid.UUID | None,
    district_id: uuid.UUID | None,
    db: Session,
) -> None:
    """Reject a scope assignment that reaches outside the caller's own jurisdiction.

    The super-admin may assign anything. Everyone else may only assign a scope
    contained within their own, verified by walking Ward → Taluka → District
    rather than trusting the three FK columns to be mutually consistent.

    Without this, ``_user_scope_filter`` was the only gate on ``PUT
    /admin/admins/{id}`` — and for a taluka_admin it returns ``User.taluka_id ==
    <own taluka>``, a predicate the caller themselves satisfies. So a
    taluka_admin could target their *own* id and move themselves into a
    different taluka, which the filter then happily accepted on the next request.
    """
    if caller.role == "admin":
        return

    if district_id is not None:
        if caller.role != "district_admin" or district_id != caller.district_id:
            raise HTTPException(
                status_code=403,
                detail="You cannot assign a district outside your own jurisdiction",
            )

    if taluka_id is not None:
        taluka = db.query(Taluka).filter(Taluka.id == taluka_id).first()
        if not taluka:
            raise HTTPException(status_code=404, detail="Taluka not found")
        if caller.role == "district_admin":
            if taluka.district_id != caller.district_id:
                raise HTTPException(status_code=403, detail="That taluka is outside your district")
        elif caller.role == "taluka_admin":
            if taluka_id != caller.taluka_id:
                raise HTTPException(status_code=403, detail="That taluka is outside your jurisdiction")
        else:
            raise HTTPException(status_code=403, detail="You cannot assign a taluka")

    if ward_id is not None:
        ward = db.query(Ward).filter(Ward.id == ward_id).first()
        if not ward:
            raise HTTPException(status_code=404, detail="Ward not found")
        taluka = db.query(Taluka).filter(Taluka.id == ward.taluka_id).first()
        if caller.role == "ward_admin":
            if ward_id != caller.ward_id:
                raise HTTPException(status_code=403, detail="That ward is outside your jurisdiction")
        elif caller.role == "taluka_admin":
            if not taluka or taluka.id != caller.taluka_id:
                raise HTTPException(status_code=403, detail="That ward is outside your taluka")
        elif caller.role == "district_admin":
            if not taluka or taluka.district_id != caller.district_id:
                raise HTTPException(status_code=403, detail="That ward is outside your district")


def _get_scoped_worker(worker_id: uuid.UUID, admin_user: User, db: Session) -> User:
    """Fetch a worker by ID, scoped to the admin's geographic area."""
    query = db.query(User).filter(User.id == worker_id, User.role == "worker")
    scope = user_scope_filter(admin_user)
    if scope:
        query = query.filter(*scope)
    worker = query.first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return worker


def _announcement_out(ann: Announcement) -> dict:
    return {
        "id": str(ann.id),
        "title": ann.title,
        "body": ann.body,
        "scope": ann.scope,
        "ward_id": str(ann.ward_id) if ann.ward_id else None,
        "taluka_id": str(ann.taluka_id) if ann.taluka_id else None,
        "district_id": str(ann.district_id) if ann.district_id else None,
        "expires_at": ann.expires_at.isoformat() if ann.expires_at else None,
        "created_at": ann.created_at.isoformat() if ann.created_at else None,
        "updated_at": ann.updated_at.isoformat() if ann.updated_at else None,
        # Non-null means the push fan-out has already run. The console needs
        # this to tell an editor that their change will not reach anyone who
        # received the original — editing deliberately does not re-notify.
        "push_dispatched_at": (
            ann.push_dispatched_at.isoformat() if ann.push_dispatched_at else None
        ),
        "author_id": str(ann.author_id),
        "location_lat": ann.location_lat,
        "location_lng": ann.location_lng,
    }


def _flag_out(f: IssueFlag) -> dict:
    return {
        "id": str(f.id),
        "reporter_id": str(f.reporter_id),
        "issue_id": str(f.issue_id) if f.issue_id else None,
        "comment_id": str(f.comment_id) if f.comment_id else None,
        "reason": f.reason,
        "details": f.details,
        "status": f.status,
        "created_at": f.created_at.isoformat() if f.created_at else None,
    }


def _queue_announcement_push(ann: Announcement) -> None:
    """Mark an announcement for fan-out by the ``jobs`` service.

    Deliberately does nothing but leave ``push_dispatched_at`` NULL — the actual
    delivery happens in ``run_announcement_push_pass`` in app/main.py. See the
    note on ``Announcement.push_dispatched_at`` for why this is not done inline.
    """
    ann.push_dispatched_at = None
