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

import uuid
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import apply_admin_scope, require_any_admin, require_role
from app.core.logger import get_logger
from app.database import get_db
from app.models.announcement import Announcement
from app.models.geofence import Geofence
from app.models.issue import Issue
from app.models.issue_flag import IssueFlag
from app.models.user import User
from app.schemas.admin import (
    AssignWorker, CreateWorker, CreateGeofenceRequest, DashboardStats, 
    GeofenceListResponse, GeofenceResponse, UpdateWorker, UpdateSubAdmin, UpdateGeofenceRequest
)
from app.schemas.auth import UserResponse
from app.schemas.issue import IssueListResponse, IssueResponse
from app.services.utils import (
    apply_not_deleted_filter,
    apply_active_filter,
    get_users_by_role,
    count_issues_by_status,
)

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

@router.get("/issues", response_model=IssueListResponse)
def list_all_issues(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    status_filter: Optional[str] = Query(None, alias="status"),
    issue_type: Optional[str] = Query(None),
    ward: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    priority: Optional[str] = Query(None, description="Filter by priority: urgent/high/medium/low"),
    department: Optional[str] = Query(None, description="Filter by department"),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """List all issues in the admin's scope with optional filters (paginated).

    **Roles**: any admin.
    """
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

        total = query.count()
        items = query.order_by(Issue.created_at.desc(), Issue.id.desc()).offset((page - 1) * size).limit(size).all()
    except Exception as e:
        logger.error(f"Error listing issues: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch issues.")

    return IssueListResponse(
        items=[IssueResponse.model_validate(i) for i in items],
        total=total, page=page, size=size,
    )


@router.post("/issues/{issue_id}/reassign", response_model=IssueResponse)
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
    scope = _user_scope_filter(current_user)
    if scope and not all(getattr(worker, attr.key) == val for attr, val in zip(scope, scope)):
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
    return IssueResponse.model_validate(issue)


@router.post("/issues/{issue_id}/unblock", response_model=IssueResponse)
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
        issue.unblocked_at = datetime.utcnow()
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
    
    return IssueResponse.model_validate(issue)


@router.get("/blocked-tasks", response_model=IssueListResponse)
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
        resp = IssueResponse.model_validate(issue)
        if issue.blocked_at:
            blocked_duration = (datetime.now(timezone.utc) - issue.blocked_at).total_seconds() / 3600
            resp.blocked_duration_hours = round(blocked_duration, 1)
        responses.append(resp)
    
    return IssueListResponse(items=responses, total=total, limit=limit, offset=offset)


@router.post("/issues/{issue_id}/respond-to-block", response_model=IssueResponse)
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
    
    return IssueResponse.model_validate(issue)


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
            issue.unblocked_at = datetime.utcnow()
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


@router.post("/issues/{issue_id}/assign", response_model=IssueResponse)
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
    scope = _user_scope_filter(current_user)
    if scope and not all(getattr(worker, attr.key) == val for attr, val in zip(scope, scope)):
        raise HTTPException(status_code=403, detail="Worker is not in your administrative jurisdiction")
    
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
    return IssueResponse.model_validate(issue)


@router.post("/issues/{issue_id}/escalate", response_model=IssueResponse)
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
        issue.escalated_at = datetime.utcnow()
        db.commit()
        db.refresh(issue)
    except Exception as e:
        logger.error(f"Error escalating issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to escalate issue.")

    logger.info(f"Issue {issue_id} escalated by admin {current_user.id}")
    return IssueResponse.model_validate(issue)


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
                issue.escalated_at = datetime.utcnow()
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

        assigned_count = 0
        skipped_count = 0
        for issue in open_issues:
            if auto_assign(issue, db):
                db.commit()
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
                assigned_count += 1
            else:
                skipped_count += 1
    except Exception as e:
        logger.error(f"Auto-assign open issues error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Auto-assignment failed.")

    logger.info(
        f"Admin {current_user.id} triggered auto-assign: "
        f"{assigned_count} assigned, {skipped_count} skipped, {total_open} total open"
    )
    return {"assigned": assigned_count, "skipped": skipped_count, "total_open": total_open}


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
        issue.deleted_at = datetime.utcnow()
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
        since = datetime.utcnow() - timedelta(days=days)
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
    ward: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """List workers in the admin's scope. **Roles**: any admin."""
    try:
        query_results = get_users_by_role("worker", db)
        query = db.query(User).filter(User.id.in_([u.id for u in query_results]))

        # Scope by location FK when available
        scope = _user_scope_filter(current_user)
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


@router.post("/workers", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_worker(
    body: CreateWorker,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Create a new worker account.

    ward_admin can only create workers for their own ward.
    taluka_admin/district_admin/admin can assign any ward.

    **Roles**: any admin.
    """
    try:
        existing = db.query(User).filter(User.phone == body.phone).first()
        if existing:
            raise HTTPException(status_code=409, detail="Phone number already registered")

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

        worker = User(
            phone=body.phone,
            name=body.name,
            ward=body.ward,
            ward_id=ward_id,
            taluka_id=taluka_id,
            district_id=district_id,
            department=getattr(body, "department", None),
            role="worker",
        )
        db.add(worker)
        db.commit()
        db.refresh(worker)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating worker: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create worker account.")

    logger.info(f"Admin {current_user.id} created worker {worker.id} ({body.phone})")
    return UserResponse.model_validate(worker)


@router.put("/workers/{worker_id}", response_model=UserResponse)
def update_worker(
    worker_id: uuid.UUID,
    body: UpdateWorker,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Update a worker's profile. **Roles**: any admin (scoped)."""
    query = db.query(User).filter(User.id == worker_id, User.role == "worker")
    scope = _user_scope_filter(current_user)
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
        db.commit()
        db.refresh(worker)
    except Exception as e:
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
        query_results = get_users_by_role("worker", db)
        query = db.query(User).filter(User.id.in_([u.id for u in query_results]))
        scope = _user_scope_filter(current_user)
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
        query_results = get_users_by_role("worker", db)
        query = db.query(User).filter(User.id.in_([u.id for u in query_results]))
        scope = _user_scope_filter(current_user)
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
    since = datetime.utcnow() - timedelta(days=days)

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
        scope = _user_scope_filter(current_user)
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
    scope = _user_scope_filter(current_user)
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
    scope = _user_scope_filter(current_user)
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
    scope = _user_scope_filter(current_user)
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
    - ``district_admin`` can create ``taluka_admin`` in their district.
    - ``taluka_admin`` can create ``ward_admin`` in their taluka.

    **Roles**: admin, district_admin, taluka_admin.
    """
    allowed_role_creation = {
        "admin": {"ward_admin", "taluka_admin", "district_admin"},
        "district_admin": {"taluka_admin", "ward_admin"},
        "taluka_admin": {"ward_admin"},
    }
    if body.role not in allowed_role_creation.get(current_user.role, set()):
        raise HTTPException(
            status_code=403,
            detail=f"Your role ({current_user.role}) cannot create a {body.role}.",
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

    logger.info(f"Admin {current_user.id} created {body.role}: {sub_admin.id}")
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
    scope = _user_scope_filter(current_user)
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

    # Check if current user has permission to edit this admin
    scope = _user_scope_filter(current_user)
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

    After creation, sends push notifications to all matching citizens with FCM tokens.

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
        _push_announcement(ann, db)
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
                (Announcement.expires_at.is_(None)) | (Announcement.expires_at > datetime.utcnow())
            )

        total = query.count()
        items = query.order_by(Announcement.created_at.desc()).offset((page - 1) * size).limit(size).all()
    except Exception as e:
        logger.error(f"Error listing announcements: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch announcements.")

    return {"items": [_announcement_out(a) for a in items], "total": total, "page": page, "size": size}


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
    _current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """List content flags/reports for moderation. **Roles**: any admin."""
    try:
        query = db.query(IssueFlag)
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
    flag = db.query(IssueFlag).filter(IssueFlag.id == flag_id).first()
    if not flag:
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
    interval_days: int = Query(1, description="Interval in days for each snapshot"),
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

        now = datetime.utcnow()
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

        issue = db.query(Issue).filter(Issue.id == issue_id)
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

@router.get("/geofences", response_model=GeofenceListResponse)
def list_geofences(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """List all geofences with pagination.
    
    **Roles**: admin or district_admin.
    
    Args:
        page: Page number (1-indexed), default 1
        size: Items per page (1-200), default 20
    
    Returns:
        GeofenceListResponse with items, total, page, size
    """
    try:
        query = db.query(Geofence)
        total = query.count()
        
        geofences = (
            query
            .order_by(Geofence.created_at.desc(), Geofence.id.desc())
            .offset((page - 1) * size)
            .limit(size)
            .all()
        )
        
        items = []
        for gf in geofences:
            items.append(GeofenceResponse(
                id=gf.id,
                name=gf.name,
                latitude=gf.latitude,
                longitude=gf.longitude,
                radius_km=gf.radius_km,
                created_by_name=gf.created_by.name if gf.created_by else None,
                created_at=gf.created_at,
            ))
        
        return GeofenceListResponse(
            items=items,
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
    
    **Roles**: admin or district_admin.
    
    Args:
        body: CreateGeofenceRequest with name, latitude, longitude, radius_km
    
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
        
        # Create geofence
        geofence = Geofence(
            id=uuid.uuid4(),
            name=body.name.strip(),
            latitude=body.latitude,
            longitude=body.longitude,
            radius_km=body.radius_km,
            created_by_id=current_user.id,
        )
        
        db.add(geofence)
        db.commit()
        db.refresh(geofence)
        
        logger.info(f"Admin {current_user.id} created geofence {geofence.id}: {geofence.name}")
        
        return GeofenceResponse(
            id=geofence.id,
            name=geofence.name,
            latitude=geofence.latitude,
            longitude=geofence.longitude,
            radius_km=geofence.radius_km,
            created_by_name=current_user.name,
            created_at=geofence.created_at,
        )
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
    
    **Roles**: admin or district_admin.
    
    Args:
        geofence_id: UUID of the geofence to update
        body: UpdateGeofenceRequest with optional fields
    
    Returns:
        Updated GeofenceResponse
    """
    try:
        geofence = db.query(Geofence).filter(Geofence.id == geofence_id).first()
        if not geofence:
            raise HTTPException(status_code=404, detail="Geofence not found")
        
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
        
        return GeofenceResponse(
            id=geofence.id,
            name=geofence.name,
            latitude=geofence.latitude,
            longitude=geofence.longitude,
            radius_km=geofence.radius_km,
            created_by_name=geofence.created_by.name if geofence.created_by else None,
            created_at=geofence.created_at,
        )
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating geofence {geofence_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update geofence.")


@router.delete("/geofences/{geofence_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_geofence(
    geofence_id: uuid.UUID,
    current_user: User = Depends(require_any_admin),
    db: Session = Depends(get_db),
):
    """Delete a geofence zone.
    
    **Roles**: admin or district_admin.
    
    Args:
        geofence_id: UUID of the geofence to delete
    """
    try:
        geofence = db.query(Geofence).filter(Geofence.id == geofence_id).first()
        if not geofence:
            raise HTTPException(status_code=404, detail="Geofence not found")
        
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
        since = datetime.utcnow() - timedelta(days=days)
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


@router.post("/notifications/geofence", response_model=dict, status_code=status.HTTP_200_OK)
def send_geofence_notification(
    latitude: float = Query(..., description="Center latitude"),
    longitude: float = Query(..., description="Center longitude"),
    radius_km: float = Query(..., description="Radius in kilometers"),
    title: str = Query(..., description="Notification title"),
    body: str = Query(..., description="Notification body"),
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
    
    **Parameters**:
    - `latitude`, `longitude`: Center point of geofence
    - `radius_km`: Circular radius in kilometers
    - `title`, `body`: Notification content
    
    **Returns**: Count of users notified
    """
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

        # Query all citizens and workers with location data and FCM tokens
        users = db.query(User).filter(
            User.is_active == True,
            User.latitude.isnot(None),
            User.longitude.isnot(None),
            User.fcm_token.isnot(None),
            User.role.in_(["citizen", "worker"]),  # Only notify citizens and workers
        ).all()

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

        return {
            "success": True,
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

def _user_scope_filter(admin_user: User) -> list:
    """Build SQLAlchemy filter conditions for User queries based on admin scope."""
    if admin_user.role == "admin":
        return []
    if admin_user.role == "district_admin" and admin_user.district_id:
        return [User.district_id == admin_user.district_id]
    if admin_user.role == "taluka_admin" and admin_user.taluka_id:
        return [User.taluka_id == admin_user.taluka_id]
    if admin_user.role == "ward_admin" and admin_user.ward_id:
        return [User.ward_id == admin_user.ward_id]
    return []


def _get_scoped_worker(worker_id: uuid.UUID, admin_user: User, db: Session) -> User:
    """Fetch a worker by ID, scoped to the admin's geographic area."""
    query = db.query(User).filter(User.id == worker_id, User.role == "worker")
    scope = _user_scope_filter(admin_user)
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


def _push_announcement(ann: Announcement, db: Session):
    """Persist DB notifications and send FCM push to all citizens matching the announcement scope."""
    try:
        from app.services.notification_service import notify

        query_results = get_users_by_role("citizen", db)
        query = db.query(User).filter(User.id.in_([u.id for u in query_results]))

        if ann.scope == "ward" and ann.ward_id:
            query = query.filter(User.ward_id == ann.ward_id)
        elif ann.scope == "taluka" and ann.taluka_id:
            query = query.filter(User.taluka_id == ann.taluka_id)
        elif ann.scope == "district" and ann.district_id:
            query = query.filter(User.district_id == ann.district_id)
        # scope == "state" → no additional filter (all citizens)

        count = 0
        for citizen in query.all():
            notify(
                db=db,
                user_id=str(citizen.id),
                title=ann.title,
                body=ann.body,
                notification_type="system",
                fcm_token=citizen.fcm_token,
                location_lat=ann.location_lat,
                location_lng=ann.location_lng,
            )
            count += 1

        logger.info(f"Announcement {ann.id} push sent to {count} citizens")
    except Exception as e:
        logger.warning(f"Announcement push failed (non-fatal): {e}")
