"""
Geo Routing Service
===================
Finds the nearest available worker and auto-assigns issues using GPS data.

Uses a composite scoring function that balances proximity and workload.

Composite score (lower = better):
    score = distance_km + (active_task_count × WORKLOAD_PENALTY_KM)

``WORKLOAD_PENALTY_KM`` is treated as an equivalent distance penalty per
active task.  With the default value of 1.0 km, a worker 2 km away with
0 active tasks (score = 2.0) beats a worker 1 km away with 3 active tasks
(score = 1 + 3 = 4.0).

Search priority:
1. Same-ward workers with matching department + GPS → lowest composite score.
2. Same-ward workers with matching department, no GPS → fewest active tasks.
3. Any online + available worker with matching department + GPS → lowest composite score.
4. Any online + available worker (no department match) with GPS → lowest composite score.
5. Any online + available worker → fewest active tasks (fallback).

Department routing:
If the issue has a ``department`` set, workers with a matching ``department``
are preferred. Workers with no department set (None) are included as fallback.

Availability:
Only workers with ``is_available=True`` AND ``is_online=True`` are considered.
This allows workers to pause assignments (break, lunch) without going offline.

Shift awareness:
If a worker has active shifts configured, the current time is checked against
their shift for today. Workers outside their shift window are deprioritized
(still used as final fallback if no one else available).
"""

import math
import uuid
from datetime import datetime
from typing import Optional, List

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core.logger import get_logger

logger = get_logger("geo")

# Distance penalty (km) added to score per active task a worker already holds.
# Increase to spread work more aggressively at the cost of distance optimality.
WORKLOAD_PENALTY_KM = 1.0

# Hard cap on active tasks — workers at or above this threshold are skipped
# unless they are the only option.
MAX_ACTIVE_TASKS = 10


def _active_task_count(worker_id, db: Session) -> int:
    """Return the number of non-terminal tasks currently assigned to a worker."""
    from app.models.issue import Issue
    return (
        db.query(func.count(Issue.id))
        .filter(
            Issue.assigned_worker_id == worker_id,
            Issue.status.in_(["assigned", "in_progress"]),
        )
        .scalar() or 0
    )


def find_nearest_worker(
    lat: float,
    lng: float,
    ward: Optional[str],
    db: Session,
    exclude_worker_ids: Optional[List[uuid.UUID]] = None,
    exclude_worker_id: Optional[uuid.UUID] = None,  # kept for backwards compat
    department: Optional[str] = None,
) -> Optional[object]:
    """Find the best available worker using a composite distance + workload score.

    Args:
        lat:                 Latitude of the issue location.
        lng:                 Longitude of the issue location.
        ward:                Ward string of the issue (for same-ward preference).
        db:                  SQLAlchemy database session.
        exclude_worker_ids:  List of worker UUIDs to skip (all prior rejecters).
        exclude_worker_id:   Single UUID to skip (backwards-compatible alias).
        department:          Preferred department (water/roads/etc.) for routing.

    Returns:
        User ORM object of the best worker, or None if no workers available.
    """
    from app.models.user import User

    # Merge both exclusion params
    excluded = set()
    if exclude_worker_ids:
        excluded.update(exclude_worker_ids)
    if exclude_worker_id:
        excluded.add(exclude_worker_id)

    try:
        base_query = db.query(User).filter(
            User.role == "worker",
            User.is_online == True,
            User.is_available == True,
            User.is_active == True,
        )
        if excluded:
            base_query = base_query.filter(User.id.notin_(excluded))

        all_candidates = base_query.all()
        if not all_candidates:
            logger.info(f"No online/available workers found (ward={ward}, dept={department})")
            return None

        # Pre-fetch active task counts for all candidates in one query
        from app.models.issue import Issue
        worker_ids = [w.id for w in all_candidates]
        rows = (
            db.query(Issue.assigned_worker_id, func.count(Issue.id))
            .filter(
                Issue.assigned_worker_id.in_(worker_ids),
                Issue.status.in_(["assigned", "in_progress"]),
            )
            .group_by(Issue.assigned_worker_id)
            .all()
        )
        task_counts = {str(row[0]): row[1] for row in rows}

        # Split overloaded workers out — only used as last resort
        normal = [w for w in all_candidates if task_counts.get(str(w.id), 0) < MAX_ACTIVE_TASKS]
        overloaded = [w for w in all_candidates if task_counts.get(str(w.id), 0) >= MAX_ACTIVE_TASKS]

        # Separate candidates by shift compliance
        now = datetime.utcnow()
        in_shift_normal, out_of_shift_normal = _split_by_shift(normal, now, db)
        in_shift_over, out_of_shift_over = _split_by_shift(overloaded, now, db)

        # Priority order: in-shift normal → out-of-shift normal → in-shift overloaded → out-of-shift overloaded
        for candidates in (in_shift_normal, out_of_shift_normal, in_shift_over, out_of_shift_over):
            result = _select_best(candidates, lat, lng, ward, department, task_counts)
            if result:
                return result

        logger.info(f"No suitable worker found (ward={ward}, dept={department})")
        return None

    except Exception as e:
        logger.error(f"Error finding nearest worker: {e}", exc_info=True)
        return None


def auto_assign(issue, db: Session) -> bool:
    """Find the best worker and assign them to the issue.

    Respects issue department for routing.
    Updates ``issue.assigned_worker_id`` and ``issue.status`` in-place.
    The caller must call ``db.commit()`` to persist changes.
    
    FIX: HIGH PRIORITY BUG #5 - No worker found case
    Now notifies admins when no worker is available for manual assignment.

    Args:
        issue: Issue ORM object to assign.
        db:    SQLAlchemy database session.

    Returns:
        True if a worker was found and assigned, False otherwise.
    """
    worker = find_nearest_worker(
        issue.latitude,
        issue.longitude,
        issue.ward,
        db,
        department=getattr(issue, "department", None),
    )
    if not worker:
        logger.warning(
            f"No available worker for issue {issue.id}",
            extra={
                "issue_id": str(issue.id),
                "ward": issue.ward,
                "department": getattr(issue, 'department', None),
                "type": issue.issue_type,
            }
        )
        
        # Notify admins for manual assignment
        try:
            from app.services.notification_service import notify_localized
            from app.models.user import User as UserModel
            
            admins = db.query(UserModel).filter(
                UserModel.role == "admin",
                UserModel.is_active == True
            ).all()
            
            for admin in admins:
                notify_localized(
                    db=db,
                    user=admin,
                    key="manual_assignment_required",
                    notification_type="alert",
                    issue_id=str(issue.id),
                    issue_id_short=str(issue.id)[:8],
                    issue_type=issue.issue_type,
                    ward=issue.ward or "unknown",
                    department=getattr(issue, 'department', None) or "none",
                )
            
            if admins:
                logger.info(
                    f"Notified {len(admins)} admin(s) for manual assignment of issue {issue.id}",
                    extra={"issue_id": str(issue.id), "admin_count": len(admins)}
                )
        except Exception as e:
            logger.error(f"Failed to notify admins of manual assignment need: {e}", exc_info=True)
        
        return False

    issue.assigned_worker_id = worker.id
    issue.status = "assigned"
    logger.info(f"Auto-assigned issue {issue.id} to worker {worker.id} (ward={worker.ward}, dept={worker.department})")
    return True


# ── Private helpers ───────────────────────────────────────────────────────────

def _composite_score(distance_km: float, active_tasks: int) -> float:
    """Lower score = better candidate."""
    return distance_km + (active_tasks * WORKLOAD_PENALTY_KM)


def _select_best(candidates: list, lat: float, lng: float, ward: Optional[str],
                 department: Optional[str], task_counts: dict):
    """Pick the best worker using ward + department preference + composite score."""
    if not candidates:
        return None

    # Filter by department preference
    dept_match = [w for w in candidates if department and w.department == department]
    dept_any = [w for w in candidates if not w.department]
    dept_candidates = dept_match or dept_any or candidates

    # Prefer same-ward workers
    same_ward = [w for w in dept_candidates if ward and w.ward == ward] if ward else []
    search_pool = same_ward if same_ward else dept_candidates

    # Score workers with GPS using composite distance + workload
    with_gps = [w for w in search_pool if w.latitude and w.longitude]
    if with_gps:
        best = min(
            with_gps,
            key=lambda w: _composite_score(
                _haversine_km(lat, lng, w.latitude, w.longitude),
                task_counts.get(str(w.id), 0),
            ),
        )
        dist = round(_haversine_km(lat, lng, best.latitude, best.longitude), 2)
        tasks = task_counts.get(str(best.id), 0)
        score = round(_composite_score(dist, tasks), 2)
        logger.info(
            f"Selected worker {best.id} (dist={dist}km, active_tasks={tasks}, "
            f"score={score}, ward={best.ward}, dept={best.department})"
        )
        return best

    # Fallback: worker with fewest active tasks (no GPS)
    if search_pool:
        best = min(search_pool, key=lambda w: task_counts.get(str(w.id), 0))
        logger.info(f"No GPS data, selecting least-loaded candidate: {best.id} "
                    f"(active_tasks={task_counts.get(str(best.id), 0)})")
        return best

    return None


def _split_by_shift(candidates: list, now: datetime, db: Session):
    """Split candidates into those currently within their shift and those outside.

    Workers with no shifts configured are considered always in-shift.

    Returns:
        (in_shift, out_of_shift) — two lists of User objects.
    """
    from app.models.worker_shift import WorkerShift

    in_shift, out_of_shift = [], []
    today_dow = now.weekday()  # 0=Monday
    current_time = now.strftime("%H:%M")

    worker_ids = [w.id for w in candidates]
    shifts = (
        db.query(WorkerShift)
        .filter(
            WorkerShift.worker_id.in_(worker_ids),
            WorkerShift.day_of_week == today_dow,
            WorkerShift.is_active == True,
        )
        .all()
    )
    shift_map = {str(s.worker_id): s for s in shifts}

    for worker in candidates:
        shift = shift_map.get(str(worker.id))
        if shift is None:
            in_shift.append(worker)
        elif shift.start_time <= current_time <= shift.end_time:
            in_shift.append(worker)
        else:
            out_of_shift.append(worker)

    return in_shift, out_of_shift


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate the great-circle distance between two GPS points in kilometers."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

