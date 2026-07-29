"""
Performance and robustness regression tests (Phase 9).
======================================================

These pin down behaviours that were not *wrong* so much as unbounded: work done
in the request path that belonged elsewhere, queries with no ceiling, and retry
logic that made failures slower without making them succeed.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError


from tests.conftest import auth_header, make_user


def _make_issue(db, reporter, **kwargs):
    from app.models.issue import Issue

    defaults = dict(
        id=uuid.uuid4(),
        reporter_id=reporter.id,
        issue_type="pothole",
        description="test",
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


# ── Announcement fan-out moved out of the request path ────────────────────────


def test_announcement_creation_does_not_notify_inline(client, super_admin, db):
    """Creating an announcement must not send anything synchronously.

    It used to be one INSERT + one COMMIT + one blocking FCM call per recipient
    inside the POST handler.
    """
    from app.models.announcement import Announcement
    from app.models.notification import Notification

    for _ in range(5):
        make_user(db, "citizen")

    before = db.query(Notification).count()
    response = client.post(
        "/admin/announcements",
        headers=auth_header(super_admin),
        json={"title": "Water cut", "body": "Supply off 9-5", "scope": "state"},
    )
    assert response.status_code in (200, 201), response.text

    assert db.query(Notification).count() == before, "notifications were sent inline"

    ann = db.query(Announcement).order_by(Announcement.created_at.desc()).first()
    assert ann is not None
    assert ann.push_dispatched_at is None, "announcement was not queued for the jobs service"


def test_jobs_pass_delivers_queued_announcements(db, super_admin):
    from app.main import run_announcement_push_pass
    from app.models.announcement import Announcement
    from app.models.notification import Notification

    citizens = [make_user(db, "citizen") for _ in range(3)]
    ann = Announcement(
        id=uuid.uuid4(), title="Road closure", body="Main St closed",
        author_id=super_admin.id, scope="state",
    )
    db.add(ann)
    db.flush()

    assert run_announcement_push_pass(db) == 1
    db.refresh(ann)
    assert ann.push_dispatched_at is not None

    notified = db.query(Notification).filter(
        Notification.user_id.in_([c.id for c in citizens])
    ).count()
    assert notified == 3


def test_dispatched_announcement_is_not_resent(db, super_admin):
    from app.main import run_announcement_push_pass
    from app.models.announcement import Announcement

    make_user(db, "citizen")
    ann = Announcement(
        id=uuid.uuid4(), title="x", body="y", author_id=super_admin.id, scope="state"
    )
    db.add(ann)
    db.flush()

    assert run_announcement_push_pass(db) == 1
    assert run_announcement_push_pass(db) == 0, "already-dispatched announcement was resent"


# ── Bounded query parameters ──────────────────────────────────────────────────


def test_interval_days_zero_is_rejected(client, super_admin):
    """`current += timedelta(days=0)` never advanced — the loop OOM-killed the pod."""
    response = client.get(
        "/admin/heatmap/timemachine",
        headers=auth_header(super_admin),
        params={"start_date": "2026-01-01", "end_date": "2026-01-02", "interval_days": 0},
    )
    assert response.status_code == 422


def test_geofence_notification_radius_is_bounded(client, super_admin):
    """radius_km=99999 push-notified every user in the country."""
    response = client.post(
        "/admin/notifications/geofence",
        headers=auth_header(super_admin),
        params={
            "latitude": 23.02, "longitude": 72.57, "radius_km": 99999,
            "title": "spam", "body": "spam",
        },
    )
    assert response.status_code == 422


def test_comment_listing_is_bounded(client, citizen, db):
    """Was an unbounded .all() that then built the reply tree in memory."""
    from app.models.issue_comment import IssueComment

    issue = _make_issue(db, citizen)
    for i in range(30):
        db.add(IssueComment(
            id=uuid.uuid4(), issue_id=issue.id, author_id=citizen.id,
            body=f"comment {i}", is_internal=False,
        ))
    db.flush()

    response = client.get(
        f"/issues/{issue.id}/comments", headers=auth_header(citizen), params={"limit": 10}
    )
    assert response.status_code == 200
    assert len(response.json()) <= 10


# ── Geofence radius limits agree ──────────────────────────────────────────────


def test_geofence_radius_limits_are_consistent():
    """Three ceilings used to disagree: 5000 km, ~39.9 km implied by area, and 100 km.

    A 60 km zone passed two and was rejected by the third with a message about
    area, for a field documented as a radius with a 100 km limit.
    """

    from app.services.geofence_utils import (
        MAX_GEOFENCE_RADIUS_KM,
        validate_geofence_area,
        validate_geofence_radius,
    )

    at_limit = MAX_GEOFENCE_RADIUS_KM
    validate_geofence_radius(at_limit)
    validate_geofence_area(at_limit)  # must not contradict the radius check

    with pytest.raises(ValueError):
        validate_geofence_radius(at_limit + 1)


def test_geofence_schema_matches_the_validator():
    from pydantic import ValidationError

    from app.schemas.admin import CreateGeofenceRequest
    from app.services.geofence_utils import MAX_GEOFENCE_RADIUS_KM

    with pytest.raises(ValidationError):
        CreateGeofenceRequest(
            name="too big", latitude=23.0, longitude=72.0,
            radius_km=MAX_GEOFENCE_RADIUS_KM + 1,
        )


# ── Retry classification ──────────────────────────────────────────────────────


def test_permanent_errors_are_not_retried():
    """IntegrityError is a subclass of DatabaseError, which was listed as transient.

    A unique-constraint violation cannot succeed on a retry — it just cost
    ~1.5 s and a pooled connection before failing anyway.
    """
    from app.services.retry_service import RetryConfig, execute_with_retry

    calls = {"n": 0}

    def _always_conflicts():
        calls["n"] += 1
        raise IntegrityError("stmt", {}, Exception("duplicate key"))

    with pytest.raises(IntegrityError):
        execute_with_retry(_always_conflicts, config=RetryConfig(max_retries=3, initial_delay=0))

    assert calls["n"] == 1, f"permanent error was retried {calls['n']} times"


def test_transient_errors_are_still_retried():
    from app.services.retry_service import RetryConfig, execute_with_retry

    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OperationalError("stmt", {}, Exception("connection reset"))
        return "recovered"

    result = execute_with_retry(_flaky, config=RetryConfig(max_retries=3, initial_delay=0))
    assert result == "recovered"
    assert calls["n"] == 3


def test_retry_rolls_back_the_session_between_attempts():
    """After an OperationalError the Session is invalidated.

    Retrying on it without a rollback raises PendingRollbackError rather than
    recovering — so every "retry" was guaranteed to fail differently.
    """
    from app.services.retry_service import RetryConfig, execute_with_retry

    class _FakeSession:
        def __init__(self):
            self.rollbacks = 0

        def rollback(self):
            self.rollbacks += 1

    session = _FakeSession()
    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise OperationalError("stmt", {}, Exception("server closed connection"))
        return "ok"

    execute_with_retry(
        _flaky, config=RetryConfig(max_retries=3, initial_delay=0), session=session
    )
    assert session.rollbacks >= 1, "session was not rolled back before the retry"


# ── Pool monitor shutdown ─────────────────────────────────────────────────────


def test_pool_monitor_stops_without_waiting_out_the_interval():
    """`threading.Event().wait(...)` built a fresh Event each loop.

    Nothing could ever set it, so stop() flipped a flag the sleeping thread
    could not see and join(timeout=5) always timed out.
    """
    import time

    from app.services.pool_monitor import PoolHealthCheckThread

    thread = PoolHealthCheckThread(interval_seconds=300)
    thread.start()
    time.sleep(0.3)

    started = time.monotonic()
    thread.stop()
    thread.join(timeout=5)

    assert not thread.is_alive(), "monitor thread did not stop"
    assert time.monotonic() - started < 2, "stop() waited out the sleep interval"


def test_pool_health_handles_non_numeric_stats():
    """database.py yields the string 'N/A' when the pool lacks an attribute.

    `'N/A' < pool_size * 0.8` raised TypeError, so the endpoint reported
    "unknown" instead of a real assessment.
    """
    from app.services.pool_monitor import assess_pool_health

    result = assess_pool_health(
        {"checked_out": "N/A", "pool_size": 5, "overflow": "N/A"}
    )
    assert result["status"] in ("healthy", "warning", "critical")


# ── Transcription contract ────────────────────────────────────────────────────


def test_transcription_returns_501_when_unconfigured(client, citizen, monkeypatch):
    """Returned 200 with an empty transcript — indistinguishable from silence."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "OPENAI_API_KEY", "", raising=False)
    response = client.post(
        "/issues/transcribe",
        headers=auth_header(citizen),
        files={"audio": ("note.m4a", b"x" * 2000, "audio/m4a")},
    )
    assert response.status_code == 501


def test_transcription_rejects_mismatched_extension(client, citizen):
    """Was `ext not in ALLOWED and ctype not in ALLOWED` — rejected only if both failed."""
    response = client.post(
        "/issues/transcribe",
        headers=auth_header(citizen),
        files={"audio": ("payload.exe", b"x" * 2000, "audio/mpeg")},
    )
    assert response.status_code == 400
