"""
Leaderboards can be asked for a time window.
============================================

``get_leaderboard`` was a lifetime ``SUM(points) GROUP BY user_id`` with no way
to narrow it, which makes the board unwinnable: an account active since launch
cannot be caught, so the ranking stops motivating anyone who joins later. The
admin console had period tabs wired to nothing for exactly this reason.

``RewardTransaction.created_at`` is timezone-aware and indexed, so the window is
one filter on an index rather than a scan.

Two things are deliberately *not* windowed, and are pinned below:

* **Level and badge count stay lifetime.** A badge earned last year is not
  un-earned by asking for this week's board, and a row reading "Level 4, 30
  points" would be nonsense.
* **A worker's ``tasks_completed`` and ``avg_rating`` *are* windowed**, keyed on
  ``resolved_at``, because status carries no date. Without that the two halves
  of a single row disagree — this week's points beside a lifetime task count.
"""

from datetime import timedelta

from app.core.time import now_utc
from app.models.reward import RewardTransaction

from tests.conftest import auth_header, make_user


def _award(db, user, points, *, days_ago=0, event_type="report_issue"):
    """Write a reward transaction at a chosen point in the past."""
    row = RewardTransaction(
        user_id=user.id,
        event_type=event_type,
        points=points,
        created_at=now_utc() - timedelta(days=days_ago),
    )
    db.add(row)
    db.flush()
    return row


def _board(client, caller, role="citizens", **params):
    response = client.get(
        f"/leaderboard/{role}", params=params, headers=auth_header(caller)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _entry(board, user):
    return next((e for e in board if e["user_id"] == str(user.id)), None)


# ── the window itself ─────────────────────────────────────────────────────────


def test_all_time_is_still_the_default(client, db, citizen):
    """Omitting `days` must not change existing behaviour."""
    veteran = make_user(db, "citizen", name="Veteran")
    _award(db, veteran, 500, days_ago=200)

    board = _board(client, citizen, limit=50)
    assert _entry(board, veteran)["total_points"] == 500


def test_window_excludes_older_points(client, db, citizen):
    veteran = make_user(db, "citizen", name="Veteran")
    _award(db, veteran, 500, days_ago=200)
    _award(db, veteran, 20, days_ago=2)

    board = _board(client, citizen, limit=50, days=7)
    assert _entry(board, veteran)["total_points"] == 20


def test_window_changes_the_ranking(client, db, citizen):
    """The point of the feature: a newcomer can top a weekly board."""
    veteran = make_user(db, "citizen", name="Veteran")
    newcomer = make_user(db, "citizen", name="Newcomer")
    _award(db, veteran, 500, days_ago=200)
    _award(db, veteran, 10, days_ago=1)
    _award(db, newcomer, 90, days_ago=1)

    all_time = _board(client, citizen, limit=50)
    assert _entry(all_time, veteran)["rank"] < _entry(all_time, newcomer)["rank"]

    weekly = _board(client, citizen, limit=50, days=7)
    assert _entry(weekly, newcomer)["rank"] < _entry(weekly, veteran)["rank"]


def test_a_user_with_no_points_in_the_window_drops_off(client, db, citizen):
    dormant = make_user(db, "citizen", name="Dormant")
    _award(db, dormant, 500, days_ago=100)

    assert _entry(_board(client, citizen, limit=50), dormant) is not None
    assert _entry(_board(client, citizen, limit=50, days=7), dormant) is None


def test_ranks_are_contiguous_within_the_window(client, db, citizen):
    """Rank is positional over the windowed rows, not carried over from all-time."""
    for i in range(3):
        u = make_user(db, "citizen", name=f"Active {i}")
        _award(db, u, 10 * (i + 1), days_ago=1)
        _award(db, u, 900, days_ago=300)

    board = _board(client, citizen, limit=50, days=7)
    ranks = [e["rank"] for e in board]
    assert ranks == sorted(ranks)
    assert ranks == list(range(1, len(ranks) + 1))


# ── what stays lifetime ───────────────────────────────────────────────────────


def test_badges_stay_lifetime(client, db, citizen):
    """A badge earned last year is not un-earned by a weekly view."""
    from app.models.reward import UserBadge

    veteran = make_user(db, "citizen", name="Veteran")
    _award(db, veteran, 500, days_ago=200)
    _award(db, veteran, 5, days_ago=1)
    db.add(UserBadge(user_id=veteran.id, badge_key="first_report"))
    db.flush()

    weekly = _entry(_board(client, citizen, limit=50, days=7), veteran)
    assert weekly["total_points"] == 5
    assert weekly["badge_count"] == 1, "badge count must not be filtered by the window"
    assert weekly["level"] >= 1


# ── worker rows stay internally consistent ────────────────────────────────────


def test_worker_task_counts_follow_the_same_window(client, db, citizen, hierarchy):
    """Otherwise a row reads: 5 points this week, 40 tasks completed ever."""
    from tests.test_phase8_endpoints import _make_issue

    worker = make_user(db, "worker", name="Windowed Worker")
    _award(db, worker, 5, days_ago=1, event_type="resolve_issue")

    _make_issue(
        db, citizen, assigned_worker_id=worker.id, status="resolved",
        resolved_at=now_utc() - timedelta(days=1), citizen_rating=5,
    )
    _make_issue(
        db, citizen, assigned_worker_id=worker.id, status="resolved",
        resolved_at=now_utc() - timedelta(days=200), citizen_rating=1,
    )

    all_time = _entry(_board(client, citizen, role="workers", limit=50), worker)
    assert all_time["tasks_completed"] == 2

    weekly = _entry(_board(client, citizen, role="workers", limit=50, days=7), worker)
    assert weekly["tasks_completed"] == 1, "task count must respect the window"
    assert weekly["avg_rating"] == 5.0, "the 1-star from 200 days ago is outside it"


# ── validation ────────────────────────────────────────────────────────────────


def test_days_is_bounded(client, citizen):
    for bad in (0, -1, 366):
        response = client.get(
            "/leaderboard/citizens",
            params={"days": bad},
            headers=auth_header(citizen),
        )
        assert response.status_code == 422, f"days={bad} should be rejected"
