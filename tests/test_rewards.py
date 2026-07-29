"""
Rewards regression tests (Phase 8).
===================================

The reward ledger drove badges, the leaderboard and worker recognition off a
check-then-insert with no constraint behind it, and two of its events had no
idempotency key at all.
"""

import uuid

from app.models.reward import RewardTransaction, UserBadge
from app.services.rewards_service import (
    POINT_VALUES,
    award_event,
    reference_for_period,
)

from tests.conftest import make_user


def _points(db, user):
    return sum(
        t.points
        for t in db.query(RewardTransaction).filter(RewardTransaction.user_id == user.id).all()
    )


def _count(db, user, event):
    return (
        db.query(RewardTransaction)
        .filter(RewardTransaction.user_id == user.id, RewardTransaction.event_type == event)
        .count()
    )


# ── Idempotency ───────────────────────────────────────────────────────────────


def test_same_reference_awards_once(db, citizen):
    ref = uuid.uuid4()
    award_event(db, citizen.id, "report_issue", reference_id=ref)
    award_event(db, citizen.id, "report_issue", reference_id=ref)
    assert _count(db, citizen, "report_issue") == 1


def test_different_references_award_separately(db, citizen):
    """Distinct events must not be collapsed into one another."""
    for _ in range(3):
        award_event(db, citizen.id, "report_issue", reference_id=uuid.uuid4())
    assert _count(db, citizen, "report_issue") == 3


def test_aadhaar_verification_cannot_be_farmed(db, citizen):
    """Was awarded with no reference_id, so re-running the flow banked +50 each time."""
    for _ in range(5):
        award_event(db, citizen.id, "aadhar_verified", reference_id=citizen.id)
    assert _count(db, citizen, "aadhar_verified") == 1
    assert _points(db, citizen) == POINT_VALUES["aadhar_verified"]


def test_weekly_streak_is_keyed_to_the_week(db):
    """Same week: once. Next week: again.

    The only thing preventing a repeat payout was `last_streak_date`, a variable
    in the jobs process's memory — so any restart on a Sunday re-awarded every
    eligible worker.
    """
    worker = make_user(db, "worker")
    this_week = reference_for_period("weekly_streak", worker.id, "2026-W30")
    next_week = reference_for_period("weekly_streak", worker.id, "2026-W31")

    award_event(db, worker.id, "weekly_streak", reference_id=this_week)
    award_event(db, worker.id, "weekly_streak", reference_id=this_week)
    assert _count(db, worker, "weekly_streak") == 1

    award_event(db, worker.id, "weekly_streak", reference_id=next_week)
    assert _count(db, worker, "weekly_streak") == 2


def test_reference_for_period_is_deterministic(db, citizen):
    a = reference_for_period("weekly_streak", citizen.id, "2026-W30")
    b = reference_for_period("weekly_streak", citizen.id, "2026-W30")
    c = reference_for_period("weekly_streak", citizen.id, "2026-W31")
    assert a == b
    assert a != c


def test_events_without_reference_stay_repeatable(db, citizen):
    """NULL reference_id means "genuinely repeatable" and must not self-collide."""
    award_event(db, citizen.id, "report_issue")
    award_event(db, citizen.id, "report_issue")
    assert _count(db, citizen, "report_issue") == 2


# ── Constraints exist at the database level ───────────────────────────────────


def test_duplicate_reward_row_is_rejected_by_the_database(db, citizen):
    """The application guard is a fast path; the constraint is the guarantee."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    ref = uuid.uuid4()
    for _ in range(2):
        db.add(
            RewardTransaction(
                id=uuid.uuid4(), user_id=citizen.id, points=10,
                event_type="report_issue", reference_id=ref,
            )
        )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_duplicate_badge_is_rejected_by_the_database(db, citizen):
    import pytest
    from sqlalchemy.exc import IntegrityError

    for _ in range(2):
        db.add(UserBadge(id=uuid.uuid4(), user_id=citizen.id, badge_key="first_report"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


# ── Points survive a badge failure ────────────────────────────────────────────


def test_badge_failure_does_not_discard_the_points(db, citizen, monkeypatch):
    """A failing badge check used to roll back the caller's pending transaction.

    award_event added the RewardTransaction, called _check_badge_unlocks, whose
    handler ran db.rollback() and threw the transaction away — then award_event
    committed nothing and logged "+10 pts" for points that no longer existed.
    """
    from app.services import rewards_service

    def _explode(*_args, **_kwargs):
        raise RuntimeError("badge subsystem is down")

    monkeypatch.setattr(rewards_service, "_check_badge_unlocks", _explode)

    award_event(db, citizen.id, "report_issue", reference_id=uuid.uuid4())
    assert _count(db, citizen, "report_issue") == 1, "points were lost when badges failed"


# ── Badge thresholds ──────────────────────────────────────────────────────────


def test_vote_received_accumulates_per_vote(db, citizen):
    """Keyed on the vote, so N upvotes pay N times.

    Keyed on the issue (the old behaviour) an issue with 200 upvotes earned its
    reporter one payment, and `community_voice` — documented as "50+ upvotes
    total" and gated on 250 points — needed 50 separate issues instead.
    """
    for _ in range(50):
        award_event(db, citizen.id, "vote_received", reference_id=uuid.uuid4())

    total = (
        db.query(RewardTransaction)
        .filter(
            RewardTransaction.user_id == citizen.id,
            RewardTransaction.event_type == "vote_received",
        )
        .count()
    )
    assert total == 50
    assert _points(db, citizen) == 50 * POINT_VALUES["vote_received"]

    earned = {b.badge_key for b in db.query(UserBadge).filter(UserBadge.user_id == citizen.id).all()}
    assert "community_voice" in earned, "50 upvotes should unlock community_voice"
