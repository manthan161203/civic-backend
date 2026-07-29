"""
Background jobs regression tests (Phase 7.8).
=============================================

The escalation loop had never completed a single cycle in production. It
computed ``datetime.utcnow() - issue.created_at`` — naive minus aware — which
raises ``TypeError`` on the first open issue, and a bare ``except Exception``
swallowed it. Everything downstream in the same block (weekly streak rewards,
expired-OTP purge, stale-token purge, lapsed-invitation cleanup) never ran.

These tests call the cycle directly. The loop is now only a timer around
``run_maintenance_cycle``, precisely so this is possible.
"""

import uuid
from datetime import timedelta

from app.core.time import now_utc
from app.main import run_cleanup_pass, run_escalation_pass

from tests.conftest import make_user


def _make_issue(db, reporter, *, age_hours, priority="medium", **kwargs):
    """An open issue created ``age_hours`` in the past."""
    from app.models.issue import Issue

    defaults = dict(
        id=uuid.uuid4(),
        reporter_id=reporter.id,
        issue_type="pothole",
        description="aged issue",
        latitude=23.02,
        longitude=72.57,
        status="open",
        priority=priority,
        severity="medium",
        before_photos=[],
        after_photos=[],
        created_at=now_utc() - timedelta(hours=age_hours),
    )
    defaults.update(kwargs)
    issue = Issue(**defaults)
    db.add(issue)
    db.flush()
    return issue


def test_escalation_pass_does_not_raise_on_aged_issue(db, citizen):
    """The exact operation that took the jobs service down.

    `now_utc() - issue.created_at` where created_at is a timestamptz. Under the
    old naive clock this raised:
        TypeError: can't subtract offset-naive and offset-aware datetimes
    """
    _make_issue(db, citizen, age_hours=100, priority="medium")  # SLA 72h
    run_escalation_pass(db)  # must not raise


def test_medium_issue_escalates_at_one_sla(db, citizen):
    """72h SLA — an issue 100h old is overdue and must reach level 1."""
    issue = _make_issue(db, citizen, age_hours=100, priority="medium")
    escalated = run_escalation_pass(db)
    assert escalated == 1
    db.refresh(issue)
    assert issue.is_escalated is True
    assert issue.escalation_level == 1
    assert issue.escalated_at is not None


def test_escalation_is_tiered(db, citizen):
    """2x SLA → level 2, 3x SLA → level 3."""
    two_x = _make_issue(db, citizen, age_hours=150, priority="medium")   # >= 144
    three_x = _make_issue(db, citizen, age_hours=250, priority="medium")  # >= 216
    run_escalation_pass(db)
    db.refresh(two_x)
    db.refresh(three_x)
    assert two_x.escalation_level == 2
    assert three_x.escalation_level == 3


def test_fresh_issue_is_not_escalated(db, citizen):
    issue = _make_issue(db, citizen, age_hours=1, priority="medium")
    assert run_escalation_pass(db) == 0
    db.refresh(issue)
    assert issue.is_escalated in (False, None)


def test_escalation_does_not_repeat_at_the_same_level(db, citizen):
    """A second pass must not re-escalate an issue already at its target level."""
    _make_issue(db, citizen, age_hours=100, priority="medium")
    assert run_escalation_pass(db) == 1
    assert run_escalation_pass(db) == 0


def test_urgent_issues_escalate_sooner_than_low(db, citizen):
    """Urgent SLA is 24h, low is 168h — the same age must treat them differently."""
    urgent = _make_issue(db, citizen, age_hours=30, priority="urgent")
    low = _make_issue(db, citizen, age_hours=30, priority="low")
    run_escalation_pass(db)
    db.refresh(urgent)
    db.refresh(low)
    assert urgent.escalation_level == 1
    assert low.is_escalated in (False, None)


def test_cleanup_purges_expired_otps(db):
    """The OTP table grew without bound because this code was never reached."""
    from app.models.otp import OTP

    expired = OTP(
        phone="+919000000001", code="111111", expires_at=now_utc() - timedelta(hours=2)
    )
    live = OTP(
        phone="+919000000002", code="222222", expires_at=now_utc() + timedelta(minutes=5)
    )
    db.add_all([expired, live])
    db.flush()

    result = run_cleanup_pass(db)
    assert result["otps"] >= 1

    remaining = {o.phone for o in db.query(OTP).all()}
    assert "+919000000001" not in remaining
    assert "+919000000002" in remaining


def test_cleanup_retires_lapsed_invitations(db):
    """Workers invited more than 7 days ago who never onboarded."""
    stale = make_user(
        db,
        "worker",
        is_active=False,
        must_change_password=True,
        invitation_sent_at=now_utc() - timedelta(days=10),
    )
    recent = make_user(
        db,
        "worker",
        is_active=False,
        must_change_password=True,
        invitation_sent_at=now_utc() - timedelta(days=1),
    )

    run_cleanup_pass(db)
    db.refresh(stale)
    db.refresh(recent)
    assert stale.invitation_sent_at is None
    assert recent.invitation_sent_at is not None
