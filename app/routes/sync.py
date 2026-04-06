"""
Offline Sync Routes
===================
Workers in low-connectivity areas can batch their actions offline
and sync them when back online. The sync endpoint processes queued
actions in order and returns results for each.
"""

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.core.logger import get_logger
from app.database import get_db
from app.models.issue import Issue
from app.models.user import User

logger = get_logger("sync")

router = APIRouter(prefix="/sync", tags=["Offline Sync"])


class SyncAction(BaseModel):
    """A single queued offline action.

    Attributes:
        action:    The action type — ``"accept_task"``, ``"start_task"``,
                   ``"update_location"``, ``"add_note"``.
        issue_id:  Related issue UUID (required for task actions).
        payload:   Action-specific data (e.g. latitude/longitude for location updates).
        timestamp: When the action was performed offline (ISO 8601).
        client_id: Client-generated unique ID to prevent duplicate processing.
    """
    action: str = Field(..., description="accept_task | start_task | update_location | add_note")
    issue_id: Optional[uuid.UUID] = None
    payload: dict = Field(default_factory=dict)
    timestamp: datetime
    client_id: str = Field(..., description="Client-generated unique ID for dedup")


class SyncRequest(BaseModel):
    actions: List[SyncAction] = Field(..., min_length=1, max_length=50, description="Queued offline actions (max 50)")


class SyncResult(BaseModel):
    client_id: str
    success: bool
    error: Optional[str] = None


class SyncResponse(BaseModel):
    processed: int
    results: List[SyncResult]


@router.post("", response_model=SyncResponse)
def sync_offline_actions(
    body: SyncRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Process batched offline actions from the mobile app.

    Actions are processed in order. Each action gets an individual
    success/failure result. Failed actions don't block subsequent ones.

    Supported actions:
    - ``accept_task``: Accept an assigned task (requires ``issue_id``).
    - ``start_task``: Start working on a task (requires ``issue_id``).
    - ``update_location``: Update worker GPS (requires ``payload.latitude``, ``payload.longitude``).
    - ``add_note``: Add resolution notes to a task (requires ``issue_id``, ``payload.notes``).

    **Roles**: any authenticated user (primarily workers).
    """
    results = []
    processed = 0

    for action in body.actions:
        result = SyncResult(client_id=action.client_id, success=False)
        try:
            if action.action == "accept_task":
                _sync_accept_task(db, current_user, action)
                result.success = True

            elif action.action == "start_task":
                _sync_start_task(db, current_user, action)
                result.success = True

            elif action.action == "update_location":
                _sync_update_location(db, current_user, action)
                result.success = True

            elif action.action == "add_note":
                _sync_add_note(db, current_user, action)
                result.success = True

            else:
                result.error = f"Unknown action: {action.action}"

            if result.success:
                processed += 1
        except HTTPException as e:
            result.error = e.detail
        except Exception as e:
            result.error = str(e)
            logger.warning(f"Sync action {action.action} failed for user {current_user.id}: {e}")

        results.append(result)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Sync commit failed: {e}", exc_info=True)
        return SyncResponse(processed=0, results=[
            SyncResult(client_id=r.client_id, success=False, error="Batch commit failed") for r in results
        ])

    logger.info(f"Sync: {processed}/{len(body.actions)} actions processed for user {current_user.id}")
    return SyncResponse(processed=processed, results=results)


def _sync_accept_task(db: Session, user: User, action: SyncAction):
    if not action.issue_id:
        raise HTTPException(status_code=400, detail="issue_id required")
    issue = db.query(Issue).filter(
        Issue.id == action.issue_id,
        Issue.assigned_worker_id == user.id,
    ).first()
    if not issue:
        raise HTTPException(status_code=404, detail="Task not found or not assigned to you")
    if issue.status == "assigned":
        issue.status = "assigned"  # confirm acceptance (no status change needed beyond assigned)


def _sync_start_task(db: Session, user: User, action: SyncAction):
    if not action.issue_id:
        raise HTTPException(status_code=400, detail="issue_id required")
    issue = db.query(Issue).filter(
        Issue.id == action.issue_id,
        Issue.assigned_worker_id == user.id,
    ).first()
    if not issue:
        raise HTTPException(status_code=404, detail="Task not found or not assigned to you")
    if issue.status in ("assigned", "open"):
        issue.status = "in_progress"


def _sync_update_location(db: Session, user: User, action: SyncAction):
    lat = action.payload.get("latitude")
    lng = action.payload.get("longitude")
    if lat is None or lng is None:
        raise HTTPException(status_code=400, detail="latitude and longitude required in payload")
    user.latitude = float(lat)
    user.longitude = float(lng)
    user.location_updated_at = action.timestamp


def _sync_add_note(db: Session, user: User, action: SyncAction):
    if not action.issue_id:
        raise HTTPException(status_code=400, detail="issue_id required")
    notes = action.payload.get("notes")
    if not notes:
        raise HTTPException(status_code=400, detail="notes required in payload")
    issue = db.query(Issue).filter(
        Issue.id == action.issue_id,
        Issue.assigned_worker_id == user.id,
    ).first()
    if not issue:
        raise HTTPException(status_code=404, detail="Task not found or not assigned to you")
    # Append notes
    existing = issue.resolution_notes or ""
    issue.resolution_notes = f"{existing}\n[{action.timestamp.isoformat()}] {notes}".strip()
