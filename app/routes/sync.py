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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.core.logger import get_logger
from app.database import get_db
from app.models.issue import Issue
from app.models.issue_comment import IssueComment
from app.models.sync_action import SyncedAction
from app.models.user import User

logger = get_logger("sync")

router = APIRouter(prefix="/sync", tags=["Offline Sync"])


class SyncAction(BaseModel):
    """A single queued offline action.

    Attributes:
        action:    The action type — ``"create_issue"``, ``"accept_task"``,
                   ``"start_task"``, ``"update_location"``, ``"add_note"``.
        issue_id:  Related issue UUID (required for task actions).
        payload:   Action-specific data (e.g. latitude/longitude for location updates).
        timestamp: When the action was performed offline (ISO 8601).
        client_id: Client-generated unique ID to prevent duplicate processing.
    """
    action: str = Field(
        ...,
        description="create_issue | accept_task | start_task | update_location | add_note",
    )
    issue_id: Optional[uuid.UUID] = None
    payload: dict = Field(default_factory=dict)
    timestamp: datetime
    client_id: str = Field(..., description="Client-generated unique ID for dedup")


class SyncRequest(BaseModel):
    actions: List[SyncAction] = Field(..., min_length=1, max_length=50, description="Queued offline actions (max 50)")


class SyncResult(BaseModel):
    """Outcome of one queued action.

    Attributes:
        client_id:   Echoes the action's client_id so the client can match it up.
        success:     Whether the action is now applied.
        duplicate:   True when this action had already been applied by an earlier
                     call. ``success`` is also True — the requested state holds.
        error:       A short reason when ``success`` is False.
        resource_id: UUID of anything the action created. Set for
                     ``create_issue`` so the client can then upload the photos it
                     could not send while offline, and open the issue it filed.
    """

    client_id: str
    success: bool
    duplicate: bool = False
    error: Optional[str] = None
    resource_id: Optional[uuid.UUID] = None


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
    - ``create_issue``: File a report composed offline (citizen; requires
      ``payload.issue_type``, ``payload.latitude``, ``payload.longitude``).
      Returns the new issue id in ``resource_id`` so the client can then upload
      the photos it could not send while disconnected.
    - ``accept_task``: Accept an assigned task (requires ``issue_id``).
    - ``start_task``: Start working on a task (requires ``issue_id``).
    - ``update_location``: Update worker GPS (requires ``payload.latitude``, ``payload.longitude``).
    - ``add_note``: Post an internal note on a task (requires ``issue_id``,
      ``payload.notes``). Creates an ``IssueComment`` — the same thing the
      online path does.

    **Roles**: any authenticated user (primarily workers).
    """
    handlers = {
        "create_issue": _sync_create_issue,
        "accept_task": _sync_accept_task,
        "start_task": _sync_start_task,
        "update_location": _sync_update_location,
        "add_note": _sync_add_note,
    }

    # Actions from this user already applied in an earlier call. A batch whose
    # response was lost is retried by the client — the normal case for an
    # endpoint that exists for flaky connections — and without this every action
    # in it was applied a second time.
    #
    # `resource_id` comes back too, so a replayed `create_issue` returns the id
    # of the issue the first call created rather than nothing.
    incoming_ids = [a.client_id for a in body.actions]
    already_done = {
        row.client_id: row.resource_id
        for row in db.query(SyncedAction.client_id, SyncedAction.resource_id)
        .filter(
            SyncedAction.user_id == current_user.id,
            SyncedAction.client_id.in_(incoming_ids),
        )
        .all()
    }

    results = []
    processed = 0
    seen_in_batch: dict[str, Optional[uuid.UUID]] = {}

    for action in body.actions:
        result = SyncResult(client_id=action.client_id, success=False)

        if action.client_id in already_done or action.client_id in seen_in_batch:
            # Idempotent: report success without reapplying. The client asked
            # for this action to happen, and it has.
            result.success = True
            result.duplicate = True
            result.resource_id = (
                already_done.get(action.client_id) or seen_in_batch.get(action.client_id)
            )
            results.append(result)
            continue

        handler = handlers.get(action.action)
        if handler is None:
            result.error = f"Unknown action: {action.action}"
            results.append(result)
            continue

        # Each action gets its own SAVEPOINT. Without one, an action that failed
        # part-way through — _sync_update_location raising ValueError on
        # longitude after latitude was already assigned — left the half-applied
        # mutation in the session, and the single commit at the end persisted it
        # while reporting the action as failed.
        try:
            with db.begin_nested():
                # Handlers return the UUID of anything they created, or None.
                created_id = handler(db, current_user, action)
                db.add(
                    SyncedAction(
                        user_id=current_user.id,
                        client_id=action.client_id,
                        action=action.action,
                        resource_id=created_id,
                    )
                )
            result.success = True
            result.resource_id = created_id
            processed += 1
            seen_in_batch[action.client_id] = created_id
        except HTTPException as e:
            result.error = e.detail
        except Exception as e:
            # Raw exception text goes to the log, not to the client: it carries
            # SQL fragments, column names and value details.
            logger.warning(
                "Sync action %s failed for user %s: %s",
                action.action, current_user.id, e, exc_info=True,
            )
            result.error = "Action could not be applied"

        results.append(result)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Sync commit failed: {e}", exc_info=True)
        return SyncResponse(processed=0, results=[
            SyncResult(client_id=r.client_id, success=False, error="Batch commit failed") for r in results
        ])

    # Rewards are granted only once the batch is durably committed, and outside
    # any savepoint — `award_event` commits, which inside `begin_nested()` would
    # end the batch transaction early.
    _award_synced_reports(db, current_user, body.actions, results)

    logger.info(f"Sync: {processed}/{len(body.actions)} actions processed for user {current_user.id}")
    return SyncResponse(processed=processed, results=results)


def _award_synced_reports(
    db: Session, user: User, actions: List[SyncAction], results: List[SyncResult]
) -> None:
    """Grant report_issue points for issues this batch created.

    Runs after the commit, so a reward failure cannot cost the citizen the
    report itself. `award_event` is idempotent on `reference_id`, so a replayed
    batch — which reports `duplicate` and is skipped here anyway — could not
    double-pay even if it reached this point.
    """
    created = [
        r.resource_id
        # strict=: results is built one-per-action in the loop above, so a
        # length mismatch would be a bug worth failing on, not silently truncating.
        for action, r in zip(actions, results, strict=True)
        if action.action == "create_issue" and r.success and not r.duplicate and r.resource_id
    ]
    if not created:
        return

    try:
        from app.services.rewards_service import award_event

        for issue_id in created:
            award_event(db, user.id, "report_issue", reference_id=issue_id)
    except Exception as e:  # noqa: BLE001
        # Points are not worth failing a citizen's report over — the issue is
        # already committed and visible.
        logger.warning("Rewards for %d synced report(s) failed: %s", len(created), e)


def _sync_create_issue(db: Session, user: User, action: SyncAction) -> uuid.UUID:
    """File a report a citizen composed while offline.

    Returns the new issue's UUID, which the caller puts in
    ``SyncResult.resource_id``.

    **Photos are not carried here.** They are multipart uploads and cannot ride
    in a JSON batch, so the client holds them locally and POSTs them to
    ``/issues/{id}/photos`` once it has the id this returns. An issue therefore
    arrives with ``before_photos: []`` and gains them a moment later — which also
    means AI classification, which runs on photo upload, happens then rather
    than now.

    Deliberately narrower than ``POST /issues``: no duplicate detection, no SOS
    radius broadcast, no AI pre-classification. Those depend on the report being
    fresh and the reporter still being at the location, neither of which holds
    for something queued hours ago in a basement. What it does share is the
    validation, the ward resolution and the reward.
    """
    from pydantic import ValidationError as PydanticValidationError

    from app.core.constants import (
        ISSUE_PRIORITY_DEFAULT,
        ISSUE_PRIORITY_URGENT,
        ISSUE_SEVERITY_DEFAULT,
    )
    from app.models.location import Ward
    from app.schemas.issue import IssueCreate

    if user.role not in ("citizen", "admin"):
        raise HTTPException(status_code=403, detail="Only citizens can file issues")

    # Validate through the same schema the online endpoint uses, rather than
    # hand-rolling checks here. That keeps the issue-type enum, the coordinate
    # ranges and the field limits in one place — a new issue type added to
    # IssueCreate is accepted here automatically.
    try:
        body = IssueCreate(**(action.payload or {}))
    except PydanticValidationError as e:
        first = e.errors()[0]
        field = ".".join(str(p) for p in first.get("loc", ())) or "payload"
        raise HTTPException(status_code=400, detail=f"{field}: {first.get('msg', 'invalid')}")

    resolved_ward = body.ward or user.ward
    ward_id = body.ward_id
    if ward_id:
        ward = db.query(Ward).filter(Ward.id == ward_id).first()
        if not ward:
            raise HTTPException(status_code=404, detail="Ward not found")
        resolved_ward = resolved_ward or ward.name
    else:
        ward_id = user.ward_id

    issue = Issue(
        reporter_id=user.id,
        issue_type=body.issue_type.value if hasattr(body.issue_type, "value") else body.issue_type,
        custom_issue_type_label=body.custom_issue_type_label,
        severity=body.severity or ISSUE_SEVERITY_DEFAULT,
        priority=ISSUE_PRIORITY_URGENT if body.is_sos else (body.priority or ISSUE_PRIORITY_DEFAULT),
        department=body.department,
        description=body.description,
        latitude=body.latitude,
        longitude=body.longitude,
        address=body.address,
        ward=resolved_ward,
        ward_id=ward_id,
        before_photos=[],
        after_photos=[],
        is_sos=body.is_sos,
        # Dated to when the citizen wrote it, not when the queue drained. SLA
        # escalation counts from created_at, so stamping it "now" would silently
        # forgive every hour the report spent in a pocket.
        created_at=action.timestamp,
    )
    db.add(issue)
    db.flush()  # assign the primary key

    # Read the id out now, while the savepoint is still open. Anything that
    # touches the instance after the enclosing `begin_nested()` closes triggers
    # a refresh against a finished transaction.
    #
    # The reward is NOT granted here. `award_event` commits internally, and a
    # commit inside the per-action SAVEPOINT would end the whole batch's
    # transaction — persisting every action processed so far, including ones
    # that had already been reported as failed. It is granted after the batch
    # commits instead; see `_award_synced_reports`.
    return issue.id


def _sync_accept_task(db: Session, user: User, action: SyncAction):
    if not action.issue_id:
        raise HTTPException(status_code=400, detail="issue_id required")
    issue = db.query(Issue).filter(
        Issue.id == action.issue_id,
        Issue.assigned_worker_id == user.id,
    ).first()
    if not issue:
        raise HTTPException(status_code=404, detail="Task not found or not assigned to you")
    # Accepting starts the work, exactly as POST /workers/tasks/{id}/accept does
    # online. This used to read `if status == "assigned": status = "assigned"` —
    # a no-op — so a worker who accepted a task offline came back online to find
    # it still sitting unaccepted.
    if issue.status != "assigned":
        raise HTTPException(
            status_code=400, detail="Task cannot be accepted in its current state"
        )
    issue.status = "in_progress"


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
    """Replay a note a worker wrote while offline.

    This creates an internal ``IssueComment``, which is what the online path
    (``POST /issues/{id}/comments`` with ``is_internal=true``) does.

    It used to append the text to ``Issue.resolution_notes`` instead. That made
    the same user action produce two different results depending on whether the
    worker had signal at the time: online it appeared in the comment thread with
    an author and a timestamp; offline it was concatenated into a free-text
    column that the thread does not read, so the note simply vanished from the
    place everyone looks for it — and, being the field shown to the citizen on
    resolution, it leaked internal remarks into a public one.
    """
    if not action.issue_id:
        raise HTTPException(status_code=400, detail="issue_id required")

    # `notes` is the documented key. `note` is accepted too because it is the
    # obvious singular and the error it produced ("notes required in payload")
    # gave no hint which spelling was wanted.
    notes = action.payload.get("notes") or action.payload.get("note")
    if not notes or not str(notes).strip():
        raise HTTPException(status_code=400, detail="notes required in payload")

    issue = db.query(Issue).filter(
        Issue.id == action.issue_id,
        Issue.assigned_worker_id == user.id,
    ).first()
    if not issue:
        raise HTTPException(status_code=404, detail="Task not found or not assigned to you")

    db.add(
        IssueComment(
            issue_id=issue.id,
            author_id=user.id,
            body=str(notes).strip()[:5000],
            # Worker notes are internal; the online path sets the same flag, and
            # `list_comments` hides these from citizens.
            is_internal=True,
            # Stamp it with when the worker actually wrote it, not when the
            # queue happened to drain. A note written at 09:00 and synced at
            # 14:00 belongs at 09:00 in the thread.
            created_at=action.timestamp,
        )
    )
