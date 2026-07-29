"""
Endpoint correctness regression tests (Phase 8).
================================================

Endpoints that returned 200 while doing the wrong thing, or 500 unconditionally.
"""

import uuid
from datetime import timedelta


from app.core.time import now_utc

from tests.conftest import auth_header, make_user


def _make_issue(db, reporter, **kwargs):
    from app.models.issue import Issue

    defaults = dict(
        id=uuid.uuid4(),
        reporter_id=reporter.id,
        issue_type="pothole",
        description="test issue",
        latitude=23.02,
        longitude=72.57,
        status="open",
        priority="medium",
        severity="medium",
        before_photos=[],
        after_photos=[],
    )
    defaults.update(kwargs)
    issue = Issue(**defaults)
    db.add(issue)
    db.flush()
    return issue


# ── DELETE /auth/account ──────────────────────────────────────────────────────


def test_account_deletion_works(client, db):
    """Returned 500 on every call, for every user, since the feature shipped.

    `from app.models.reward import Reward` — a class that has never existed —
    raised ImportError before any cleanup ran. Two more attribute errors
    (IssueFlag.flagger_id, WardSubscription.citizen_id) waited behind it.
    """
    user = make_user(db, "citizen")
    response = client.delete("/auth/account", headers=auth_header(user))
    assert response.status_code == 200, response.text
    db.refresh(user)
    assert user.is_active is False


def test_account_deletion_removes_reward_rows(client, db):
    from app.models.reward import RewardTransaction, UserBadge
    from app.services.rewards_service import award_event

    user = make_user(db, "citizen")
    award_event(db, user.id, "report_issue", reference_id=uuid.uuid4())
    assert db.query(RewardTransaction).filter(RewardTransaction.user_id == user.id).count() == 1

    assert client.delete("/auth/account", headers=auth_header(user)).status_code == 200
    assert db.query(RewardTransaction).filter(RewardTransaction.user_id == user.id).count() == 0
    assert db.query(UserBadge).filter(UserBadge.user_id == user.id).count() == 0


def test_account_deletion_retires_the_session(client, db):
    user = make_user(db, "citizen")
    header = auth_header(user)
    assert client.delete("/auth/account", headers=header).status_code == 200
    db.refresh(user)
    assert user.tokens_valid_from is not None


# ── Comment access control ────────────────────────────────────────────────────


def test_citizen_cannot_read_another_citizens_comments(client, citizen, other_citizen, db):
    """_check_comment_access was `pass` while three docstrings described it."""
    issue = _make_issue(db, other_citizen)
    assert client.get(f"/issues/{issue.id}/comments", headers=auth_header(citizen)).status_code == 403


def test_citizen_cannot_post_on_another_citizens_issue(client, citizen, other_citizen, db):
    issue = _make_issue(db, other_citizen)
    response = client.post(
        f"/issues/{issue.id}/comments",
        headers=auth_header(citizen),
        json={"body": "let me in"},
    )
    assert response.status_code == 403


def test_citizen_can_comment_on_own_issue(client, citizen, db):
    issue = _make_issue(db, citizen)
    response = client.post(
        f"/issues/{issue.id}/comments", headers=auth_header(citizen), json={"body": "any update?"}
    )
    assert response.status_code == 201, response.text


def test_assigned_worker_can_comment(client, citizen, worker, db):
    issue = _make_issue(db, citizen, assigned_worker_id=worker.id, status="assigned")
    response = client.post(
        f"/issues/{issue.id}/comments", headers=auth_header(worker), json={"body": "on my way"}
    )
    assert response.status_code == 201, response.text


def test_unassigned_worker_cannot_comment(client, citizen, worker, other_worker, db):
    issue = _make_issue(db, citizen, assigned_worker_id=worker.id, status="assigned")
    response = client.post(
        f"/issues/{issue.id}/comments", headers=auth_header(other_worker), json={"body": "nosy"}
    )
    assert response.status_code == 403


# ── Survey IDOR ───────────────────────────────────────────────────────────────


def test_survey_is_not_readable_by_other_citizens(client, citizen, other_citizen, db):
    """Any citizen could read any other citizen's rating and free-text feedback."""
    from app.models.satisfaction_survey import SatisfactionSurvey

    issue = _make_issue(db, other_citizen, status="resolved")
    db.add(
        SatisfactionSurvey(
            id=uuid.uuid4(), issue_id=issue.id, citizen_id=other_citizen.id,
            fully_resolved=True, speed_rating=3, would_report_again=True,
            feedback="the worker was rude",
        )
    )
    db.flush()

    response = client.get(f"/issues/{issue.id}/survey", headers=auth_header(citizen))
    assert response.status_code == 404
    assert "rude" not in response.text


def test_reporter_can_read_own_survey(client, citizen, db):
    from app.models.satisfaction_survey import SatisfactionSurvey

    issue = _make_issue(db, citizen, status="resolved")
    db.add(
        SatisfactionSurvey(
            id=uuid.uuid4(), issue_id=issue.id, citizen_id=citizen.id,
            fully_resolved=True, speed_rating=3, would_report_again=True, feedback="good",
        )
    )
    db.flush()
    assert client.get(f"/issues/{issue.id}/survey", headers=auth_header(citizen)).status_code == 200


# ── Pagination metadata ───────────────────────────────────────────────────────


def test_pagination_metadata_is_correct_for_page_size():
    """page/size callers had their values silently dropped by the schema."""
    from app.schemas.issue import IssueListResponse

    r = IssueListResponse(items=[], total=900, page=5, size=20)
    assert (r.page, r.size, r.limit, r.offset, r.pages) == (5, 20, 20, 80, 45)


def test_pagination_metadata_is_correct_for_limit_offset():
    from app.schemas.issue import IssueListResponse

    r = IssueListResponse(items=[], total=900, limit=50, offset=100)
    assert (r.page, r.size, r.limit, r.offset, r.pages) == (3, 50, 50, 100, 18)


# ── Photo limit ───────────────────────────────────────────────────────────────


def test_photo_limit_comes_from_settings():
    """`hasattr(globals(), name)` is always False, so this was hardwired to 10."""
    from app.core.config import settings

    assert isinstance(settings.MAX_PHOTOS_PER_ISSUE, int)
    assert settings.MAX_PHOTOS_PER_ISSUE > 0


# ── create_issue reachability ─────────────────────────────────────────────────


def test_create_issue_has_no_unreachable_tail():
    """90 lines sat after a try that always returns or raises."""
    import ast
    import inspect

    from app.routes import issues

    tree = ast.parse(inspect.getsource(issues))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "create_issue"
    )
    last = fn.body[-1]
    assert isinstance(last, ast.Try), "create_issue must end with its try block"
    assert isinstance(last.body[-1], ast.Return)


def test_priority_default_is_importable(client, citizen):
    """`ISSUE_PRIORITY_DEFAULT` was referenced but never imported → NameError → 500."""
    response = client.post(
        "/issues",
        headers=auth_header(citizen),
        json={
            "issue_type": "pothole",
            "description": "explicit null priority",
            "latitude": 23.02,
            "longitude": 72.57,
            "priority": None,
        },
    )
    assert response.status_code == 201, response.text


# ── Offline sync ──────────────────────────────────────────────────────────────


def _sync(client, user, actions):
    return client.post("/sync", headers=auth_header(user), json={"actions": actions})


def test_sync_accept_task_actually_starts_it(client, citizen, worker, db):
    """Was `if status == "assigned": status = "assigned"` — a no-op."""
    issue = _make_issue(db, citizen, assigned_worker_id=worker.id, status="assigned")
    response = _sync(client, worker, [{
        "action": "accept_task",
        "issue_id": str(issue.id),
        "timestamp": now_utc().isoformat(),
        "client_id": "acc-1",
    }])
    assert response.status_code == 200, response.text
    db.refresh(issue)
    assert issue.status == "in_progress"


def test_sync_replay_does_not_reapply(client, citizen, worker, db):
    """client_id is documented as a dedup key and was never checked."""
    from app.models.issue_comment import IssueComment

    issue = _make_issue(db, citizen, assigned_worker_id=worker.id, status="assigned")
    action = {
        "action": "add_note",
        "issue_id": str(issue.id),
        "payload": {"notes": "fixed the kerb"},
        "timestamp": now_utc().isoformat(),
        "client_id": "note-42",
    }

    assert _sync(client, worker, [action]).status_code == 200

    def note_count():
        return (
            db.query(IssueComment)
            .filter(IssueComment.issue_id == issue.id)
            .count()
        )

    assert note_count() == 1

    second = _sync(client, worker, [action])
    assert second.status_code == 200
    assert second.json()["results"][0]["duplicate"] is True

    assert note_count() == 1, "replaying the batch posted the note twice"


def test_sync_failed_action_does_not_half_apply(client, worker, db):
    """A mid-record failure used to leave the earlier field written and committed."""
    original_lat, original_lng = worker.latitude, worker.longitude

    response = _sync(client, worker, [{
        "action": "update_location",
        "payload": {"latitude": 12.5, "longitude": "not-a-number"},
        "timestamp": now_utc().isoformat(),
        "client_id": "loc-bad",
    }])
    assert response.status_code == 200
    assert response.json()["results"][0]["success"] is False

    db.refresh(worker)
    assert worker.latitude == original_lat, "latitude was written despite the action failing"
    assert worker.longitude == original_lng


def test_sync_does_not_leak_raw_exception_text(client, worker, db):
    response = _sync(client, worker, [{
        "action": "update_location",
        "payload": {"latitude": 12.5, "longitude": "not-a-number"},
        "timestamp": now_utc().isoformat(),
        "client_id": "loc-bad-2",
    }])
    error = response.json()["results"][0]["error"]
    assert "float" not in error.lower() and "invalid literal" not in error.lower()


# ── Assignment reclaim ────────────────────────────────────────────────────────


def test_assigned_at_is_stamped_automatically(db, citizen, worker):
    """Eight code paths assign a worker; none of them set a timestamp by hand."""
    issue = _make_issue(db, citizen)
    assert issue.assigned_at is None

    issue.assigned_worker_id = worker.id
    db.flush()
    assert issue.assigned_at is not None


def test_unassigning_clears_assigned_at(db, citizen, worker):
    issue = _make_issue(db, citizen, assigned_worker_id=worker.id)
    assert issue.assigned_at is not None
    issue.assigned_worker_id = None
    db.flush()
    assert issue.assigned_at is None


def test_stale_assignment_is_reclaimed(db, citizen, worker):
    """The old worker_utils queried a `queued` status and `queued_at` column.

    Neither exists, so the AttributeError was caught and the function reported
    {"processed": 0, "failed": 0} — a clean success for something that could
    never run.
    """
    from app.main import run_assignment_reclaim_pass

    issue = _make_issue(db, citizen, assigned_worker_id=worker.id, status="assigned")
    issue.assigned_at = now_utc() - timedelta(hours=3)
    db.flush()

    assert run_assignment_reclaim_pass(db, timeout_minutes=60) == 1
    db.refresh(issue)
    assert issue.status == "open"
    assert issue.assigned_worker_id is None
    assert str(worker.id) in (issue.rejected_by_ids or [])


def test_fresh_assignment_is_not_reclaimed(db, citizen, worker):
    from app.main import run_assignment_reclaim_pass

    issue = _make_issue(db, citizen, assigned_worker_id=worker.id, status="assigned")
    db.flush()
    assert run_assignment_reclaim_pass(db, timeout_minutes=60) == 0
    db.refresh(issue)
    assert issue.status == "assigned"


def test_in_progress_work_is_not_reclaimed(db, citizen, worker):
    """A worker who started the job keeps it however long it takes."""
    from app.main import run_assignment_reclaim_pass

    issue = _make_issue(db, citizen, assigned_worker_id=worker.id, status="in_progress")
    issue.assigned_at = now_utc() - timedelta(days=5)
    db.flush()
    assert run_assignment_reclaim_pass(db, timeout_minutes=60) == 0


# ── Structured logging ────────────────────────────────────────────────────────


def test_extra_fields_reach_json_logs():
    """`extra=` sets top-level attributes; the formatter looked for custom_fields.

    So every audit field the application logged was dropped in production.
    """
    import json
    import logging

    from app.core.logger import JSONFormatter

    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="override granted", args=(), exc_info=None,
    )
    record.audit = True
    record.admin_id = "abc-123"

    payload = json.loads(JSONFormatter().format(record))
    assert payload["audit"] is True
    assert payload["admin_id"] == "abc-123"
    assert payload["message"] == "override granted"
