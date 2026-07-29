"""
Offline sync — the actions added for disconnected citizens and workers.
=======================================================================

Three gaps this covers:

1. **A citizen composing a report underground lost it.** ``POST /sync`` handled
   only worker actions, so there was nowhere to put an issue created offline.
2. **``add_note`` replay was not equivalent to the online path.** It appended to
   ``Issue.resolution_notes`` — a free-text column the comment thread does not
   read, and the one shown to the citizen on resolution — instead of creating an
   internal comment. The same user action produced two different results
   depending on whether there had been signal at the time.
3. **``start_task`` had no caller**, so the distinction between accepting a task
   and starting work on it existed only on the server.
"""

import uuid

from app.models.issue import Issue
from app.models.issue_comment import IssueComment
from app.models.reward import RewardTransaction
from app.models.sync_action import SyncedAction

from tests.conftest import auth_header

from tests.test_phase8_endpoints import _make_issue


def _sync(client, user, actions):
    return client.post("/sync", headers=auth_header(user), json={"actions": actions})


def _report(client, user, client_id, **payload_overrides):
    payload = {
        "issue_type": "pothole",
        "latitude": 23.0225,
        "longitude": 72.5714,
        "description": "Written while underground",
        **payload_overrides,
    }
    return _sync(client, user, [{
        "action": "create_issue",
        "payload": payload,
        "timestamp": "2026-07-29T08:15:00Z",
        "client_id": client_id,
    }])


# ── create_issue ──────────────────────────────────────────────────────────────


def test_offline_report_creates_an_issue(client, citizen, db):
    response = _report(client, citizen, "rep-1")
    assert response.status_code == 200, response.text

    result = response.json()["results"][0]
    assert result["success"] is True
    assert result["resource_id"], "the client needs the id to upload its photos"

    issue = db.query(Issue).filter(Issue.id == uuid.UUID(result["resource_id"])).one()
    assert issue.reporter_id == citizen.id
    assert issue.issue_type == "pothole"
    assert issue.before_photos == [], "photos cannot ride in a JSON batch"


def test_offline_report_is_dated_when_it_was_written(client, citizen, db):
    """SLA escalation counts from created_at.

    Stamping the row at drain time would silently forgive every hour the report
    spent in a pocket — a report filed at 08:15 and synced at 18:00 would look
    brand new.
    """
    response = _report(client, citizen, "rep-backdate")
    issue_id = response.json()["results"][0]["resource_id"]

    issue = db.query(Issue).filter(Issue.id == uuid.UUID(issue_id)).one()
    assert issue.created_at.isoformat().startswith("2026-07-29T08:15")


def test_offline_report_replay_returns_the_same_issue(client, citizen, db):
    """The photos are the reason this matters.

    A lost response means the client replays the batch. The dedup path
    short-circuits before any handler runs, so without a stored resource_id the
    replay would answer `duplicate` with nothing attached — and the photos held
    on the device would have no issue to attach to.
    """
    first = _report(client, citizen, "rep-replay").json()["results"][0]
    second = _report(client, citizen, "rep-replay").json()["results"][0]

    assert second["duplicate"] is True
    assert second["resource_id"] == first["resource_id"]
    assert db.query(Issue).filter(Issue.reporter_id == citizen.id).count() == 1


def test_offline_report_awards_points_once(client, citizen, db):
    response = _report(client, citizen, "rep-reward")
    issue_id = uuid.UUID(response.json()["results"][0]["resource_id"])

    _report(client, citizen, "rep-reward")  # replay

    awards = (
        db.query(RewardTransaction)
        .filter(
            RewardTransaction.user_id == citizen.id,
            RewardTransaction.event_type == "report_issue",
            RewardTransaction.reference_id == issue_id,
        )
        .count()
    )
    assert awards == 1


def test_offline_report_reward_runs_outside_the_savepoint(client, citizen, db):
    """`award_event` commits internally.

    Called inside the per-action SAVEPOINT it ended the batch transaction, which
    both persisted actions already reported as failed and left the Issue
    instance unreadable — reading `issue.id` afterwards raised "Can't operate on
    closed transaction". Two actions in one batch is what exercises that.
    """
    response = _sync(client, citizen, [
        {
            "action": "create_issue",
            "payload": {"issue_type": "pothole", "latitude": 23.0, "longitude": 72.5},
            "timestamp": "2026-07-29T08:15:00Z",
            "client_id": "multi-1",
        },
        {
            "action": "create_issue",
            "payload": {"issue_type": "garbage", "latitude": 23.1, "longitude": 72.6},
            "timestamp": "2026-07-29T08:20:00Z",
            "client_id": "multi-2",
        },
    ])
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert all(r["success"] for r in results), results
    assert len({r["resource_id"] for r in results}) == 2


def test_offline_report_validates_through_the_shared_schema(client, citizen):
    """Validation is `IssueCreate`, not hand-rolled checks in the sync module."""
    response = _report(client, citizen, "rep-bad", issue_type="unicorn")
    error = response.json()["results"][0]["error"]
    assert response.json()["results"][0]["success"] is False
    assert "issue_type" in error


def test_offline_report_rejects_out_of_range_coordinates(client, citizen):
    response = _report(client, citizen, "rep-coords", latitude=999)
    assert response.json()["results"][0]["success"] is False


def test_workers_cannot_file_issues_through_sync(client, worker):
    """`POST /issues` is citizen-only; the offline path must not be a way round."""
    response = _report(client, worker, "rep-worker")
    result = response.json()["results"][0]
    assert result["success"] is False
    assert "citizen" in (result["error"] or "").lower()


# ── add_note equivalence ──────────────────────────────────────────────────────


def test_offline_note_creates_an_internal_comment(client, citizen, worker, db):
    """The online path posts a comment. The offline replay must do the same."""
    issue = _make_issue(db, citizen, assigned_worker_id=worker.id, status="assigned")

    response = _sync(client, worker, [{
        "action": "add_note",
        "issue_id": str(issue.id),
        "payload": {"notes": "needed a jackhammer"},
        "timestamp": "2026-07-29T09:05:00Z",
        "client_id": "note-comment",
    }])
    assert response.json()["results"][0]["success"] is True

    comment = db.query(IssueComment).filter(IssueComment.issue_id == issue.id).one()
    assert comment.body == "needed a jackhammer"
    assert comment.author_id == worker.id
    assert comment.is_internal is True, "worker notes must not be visible to citizens"

    db.refresh(issue)
    assert issue.resolution_notes is None, (
        "resolution_notes is shown to the citizen on resolution — internal notes "
        "must not be concatenated into it"
    )


def test_offline_note_keeps_the_time_it_was_written(client, citizen, worker, db):
    issue = _make_issue(db, citizen, assigned_worker_id=worker.id, status="assigned")
    _sync(client, worker, [{
        "action": "add_note",
        "issue_id": str(issue.id),
        "payload": {"notes": "queued at 09:05"},
        "timestamp": "2026-07-29T09:05:00Z",
        "client_id": "note-time",
    }])

    comment = db.query(IssueComment).filter(IssueComment.issue_id == issue.id).one()
    assert comment.created_at.isoformat().startswith("2026-07-29T09:05")


def test_offline_note_accepts_either_spelling(client, citizen, worker, db):
    """`notes` is documented; `note` is the obvious singular.

    The old error — "notes required in payload" — gave no hint which was wanted,
    and the mobile client shipped with the wrong one.
    """
    issue = _make_issue(db, citizen, assigned_worker_id=worker.id, status="assigned")
    response = _sync(client, worker, [{
        "action": "add_note",
        "issue_id": str(issue.id),
        "payload": {"note": "singular spelling"},
        "timestamp": "2026-07-29T09:10:00Z",
        "client_id": "note-singular",
    }])
    assert response.json()["results"][0]["success"] is True


def test_offline_note_on_someone_elses_task_is_refused(client, citizen, worker, db):
    issue = _make_issue(db, citizen, status="open")  # not assigned to this worker
    response = _sync(client, worker, [{
        "action": "add_note",
        "issue_id": str(issue.id),
        "payload": {"notes": "not mine"},
        "timestamp": "2026-07-29T09:15:00Z",
        "client_id": "note-foreign",
    }])
    assert response.json()["results"][0]["success"] is False
    assert db.query(IssueComment).filter(IssueComment.issue_id == issue.id).count() == 0


# ── start_task ────────────────────────────────────────────────────────────────


def test_start_task_moves_an_assigned_task_into_progress(client, citizen, worker, db):
    issue = _make_issue(db, citizen, assigned_worker_id=worker.id, status="assigned")
    response = _sync(client, worker, [{
        "action": "start_task",
        "issue_id": str(issue.id),
        "timestamp": "2026-07-29T09:00:00Z",
        "client_id": "start-1",
    }])
    assert response.json()["results"][0]["success"] is True
    db.refresh(issue)
    assert issue.status == "in_progress"


def test_sync_records_the_created_resource_for_dedup(client, citizen, db):
    """The dedup row is what makes a replay able to return the same id."""
    response = _report(client, citizen, "rep-dedup-row")
    issue_id = uuid.UUID(response.json()["results"][0]["resource_id"])

    row = (
        db.query(SyncedAction)
        .filter(SyncedAction.user_id == citizen.id, SyncedAction.client_id == "rep-dedup-row")
        .one()
    )
    assert row.action == "create_issue"
    assert row.resource_id == issue_id
