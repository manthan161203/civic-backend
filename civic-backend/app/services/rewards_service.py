"""
Rewards Service — Points and Badges
=====================================
All reward logic lives here. Routes and other services call these functions
after key events occur. Every call is non-fatal — reward failures never
block the main action (issue creation, resolution, etc.).

Usage:
    from app.services.rewards_service import award_event

    # Award points for an event (also checks badge unlocks automatically):
    award_event(db, user_id=citizen.id, event="report_issue", reference_id=issue.id)
    award_event(db, user_id=worker.id, event="resolve_issue", reference_id=issue.id)

Public API:
    award_event(db, user_id, event, reference_id=None) -> None
    get_user_summary(db, user_id) -> dict
    get_leaderboard(db, role, limit) -> list[dict]
"""

import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.logger import get_logger
from app.services.utils import get_users_by_role

logger = get_logger("rewards")

# ── Point values ──────────────────────────────────────────────────────────────

POINT_VALUES: dict[str, int] = {
    # Citizen events
    "report_issue":      10,
    "issue_resolved":    20,
    "vote_received":      5,
    "rate_issue":         5,
    "aadhar_verified":   50,
    "first_report":      25,   # one-time bonus
    # Worker events
    "resolve_issue":     30,
    "five_star_rating":  25,
    "fast_resolve":      15,
    "weekly_streak":     20,
    "first_resolution":  25,   # one-time bonus
}

# ── Badge definitions ─────────────────────────────────────────────────────────
# key → {name, description, icon, check_fn_name}
# check_fn_name must match a function in _BADGE_CHECKS below.

BADGE_DEFINITIONS: dict[str, dict] = {
    # ── Citizen badges ────────────────────────────────────────────
    "first_report": {
        "name": "First Report",
        "description": "Filed your first civic issue",
        "icon": "flag",
        "role": "citizen",
    },
    "reporter_10": {
        "name": "Active Reporter",
        "description": "Filed 10 civic issues",
        "icon": "checklist",
        "role": "citizen",
    },
    "reporter_50": {
        "name": "Super Reporter",
        "description": "Filed 50 civic issues",
        "icon": "star",
        "role": "citizen",
    },
    "verified_citizen": {
        "name": "Verified Citizen",
        "description": "Completed Aadhaar KYC verification",
        "icon": "verified",
        "role": "citizen",
    },
    "community_voice": {
        "name": "Community Voice",
        "description": "Your issues have received 50+ upvotes total",
        "icon": "speaker",
        "role": "citizen",
    },
    "engaged_citizen": {
        "name": "Engaged Citizen",
        "description": "Rated 10 resolved issues",
        "icon": "star_fill",
        "role": "citizen",
    },
    # ── Worker badges ─────────────────────────────────────────────
    "first_resolution": {
        "name": "First Resolution",
        "description": "Resolved your first civic issue",
        "icon": "hammer",
        "role": "worker",
    },
    "resolver_10": {
        "name": "Dedicated Worker",
        "description": "Resolved 10 issues",
        "icon": "strength",
        "role": "worker",
    },
    "resolver_100": {
        "name": "Century Resolver",
        "description": "Resolved 100 issues",
        "icon": "trophy",
        "role": "worker",
    },
    "fast_responder": {
        "name": "Fast Responder",
        "description": "Resolved 10 issues within 24 hours",
        "icon": "bolt",
        "role": "worker",
    },
    "top_rated": {
        "name": "Top Rated",
        "description": "Received 20 five-star ratings",
        "icon": "spark",
        "role": "worker",
    },
    "streak_master": {
        "name": "Streak Master",
        "description": "Completed 4 consecutive weeks without a rejection",
        "icon": "flame",
        "role": "worker",
    },
}


# ── Main public API ───────────────────────────────────────────────────────────

def award_event(
    db: Session,
    user_id: uuid.UUID,
    event: str,
    reference_id: Optional[uuid.UUID] = None,
    note: Optional[str] = None,
) -> None:
    """Award points for a game event and check for newly unlocked badges.

    This function is non-fatal — any exception is logged and swallowed
    so that reward failures never block the main action.

    Args:
        db:           SQLAlchemy session (caller must commit after calling this
                      if they want rewards committed in the same transaction,
                      or use a separate commit here — we commit inside).
        user_id:      UUID of the user earning points.
        event:        Event key — must be in POINT_VALUES.
        reference_id: Optional related object UUID (issue, vote, etc.).
        note:         Optional human-readable note stored in the ledger.
    """
    try:
        from app.models.reward import RewardTransaction

        points = POINT_VALUES.get(event)
        if points is None:
            logger.warning(f"Unknown reward event: {event}")
            return

        tx = RewardTransaction(
            id=uuid.uuid4(),
            user_id=user_id,
            points=points,
            event_type=event,
            reference_id=reference_id,
            note=note or f"+{points} for {event}",
        )
        db.add(tx)
        db.commit()
        logger.info(f"Reward: user {user_id} +{points} pts ({event})")

        # Check if any new badges are unlocked after this transaction
        _check_badge_unlocks(db, user_id, event)

    except Exception as e:
        logger.warning(f"Reward award failed (non-fatal): {e}")
        try:
            db.rollback()
        except Exception:
            pass


def get_user_summary(db: Session, user_id: uuid.UUID) -> dict:
    """Return a user's total points, rank, recent transactions, and badges.

    Args:
        db:      SQLAlchemy session.
        user_id: UUID of the target user.

    Returns:
        Dict with keys: total_points, rank, level, level_name,
        recent_transactions (last 10), badges.
    """
    from app.models.reward import RewardTransaction, UserBadge

    total_points = (
        db.query(func.sum(RewardTransaction.points))
        .filter(RewardTransaction.user_id == user_id)
        .scalar() or 0
    )

    recent = (
        db.query(RewardTransaction)
        .filter(RewardTransaction.user_id == user_id)
        .order_by(RewardTransaction.created_at.desc())
        .limit(10)
        .all()
    )

    badges = (
        db.query(UserBadge)
        .filter(UserBadge.user_id == user_id)
        .order_by(UserBadge.earned_at.desc())
        .all()
    )

    from app.models.user import User as _User
    user_obj = db.query(_User).filter(_User.id == user_id).first()
    user_role = user_obj.role if user_obj else "citizen"
    rank = _get_rank(db, user_id, total_points, role=user_role)
    level, level_name = _points_to_level(total_points)

    return {
        "total_points": total_points,
        "rank": rank,
        "level": level,
        "level_name": level_name,
        "next_level_points": _next_level_threshold(level),
        "recent_transactions": [
            {
                "id": str(t.id),
                "points": t.points,
                "event_type": t.event_type,
                "note": t.note,
                "earned_at": t.created_at.isoformat(),
            }
            for t in recent
        ],
        "badges": [
            {
                "key": b.badge_key,
                "name": BADGE_DEFINITIONS.get(b.badge_key, {}).get("name", b.badge_key),
                "description": BADGE_DEFINITIONS.get(b.badge_key, {}).get("description", ""),
                "icon": BADGE_DEFINITIONS.get(b.badge_key, {}).get("icon", "badge"),
                "earned_at": b.earned_at.isoformat(),
            }
            for b in badges
        ],
    }


def get_leaderboard(db: Session, role: str = "citizen", limit: int = 10, offset: int = 0) -> list:
    """Return top users ranked by total points for a given role.

    Args:
        db:    SQLAlchemy session.
        role:  ``"citizen"`` or ``"worker"``.
        limit: Number of top entries to return.

    Returns:
        List of dicts with rank, user_id, name, total_points, level, badges.
    """
    from app.models.reward import RewardTransaction, UserBadge
    from app.models.user import User

    rows = (
        db.query(
            RewardTransaction.user_id,
            func.sum(RewardTransaction.points).label("total_points"),
        )
        .join(User, User.id == RewardTransaction.user_id)
        .filter(User.role == role, User.is_active == True)
        .group_by(RewardTransaction.user_id)
        .order_by(func.sum(RewardTransaction.points).desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    result = []
    user_ids = [row.user_id for row in rows]

    # Batch fetch users
    users_list = db.query(User).filter(User.id.in_(user_ids)).all() if user_ids else []
    users_map = {u.id: u for u in users_list}

    # Batch fetch badge counts
    badge_rows = (
        db.query(UserBadge.user_id, func.count(UserBadge.id))
        .filter(UserBadge.user_id.in_(user_ids))
        .group_by(UserBadge.user_id)
        .all()
    ) if user_ids else []
    badge_map = {r[0]: r[1] for r in badge_rows}

    # Batch fetch tasks completed + avg rating (workers only)
    tasks_map = {}
    rating_map = {}
    if role == "worker" and user_ids:
        from app.models.issue import Issue
        task_rows = (
            db.query(
                Issue.assigned_worker_id,
                func.count(Issue.id),
                func.avg(Issue.citizen_rating),
            )
            .filter(
                Issue.assigned_worker_id.in_(user_ids),
                Issue.status.in_(["resolved", "closed"]),
            )
            .group_by(Issue.assigned_worker_id)
            .all()
        )
        tasks_map  = {r[0]: r[1] for r in task_rows}
        rating_map = {r[0]: round(float(r[2]), 1) if r[2] else None for r in task_rows}

    # Batch resolve ward names for users whose ward string is null but have ward_id
    ward_id_users = [u for u in users_list if not u.ward and u.ward_id]
    ward_name_map = {}
    if ward_id_users:
        from app.models.location import Ward
        ward_ids = [u.ward_id for u in ward_id_users]
        ward_objs = db.query(Ward).filter(Ward.id.in_(ward_ids)).all()
        ward_name_map = {w.id: w.name for w in ward_objs}

    for rank_idx, row in enumerate(rows, start=1):
        user = users_map.get(row.user_id)
        level, level_name = _points_to_level(row.total_points)
        ward_name = (user.ward if user else None) or ward_name_map.get(user.ward_id) if user else None

        result.append({
            "rank": rank_idx,
            "user_id": str(row.user_id),
            "name": (user.name if user else None) or "Unknown",
            "ward": ward_name,
            "total_points": row.total_points,
            "level": level,
            "level_name": level_name,
            "badge_count": badge_map.get(row.user_id, 0),
            "tasks_completed": tasks_map.get(row.user_id, 0),
            "avg_rating": rating_map.get(row.user_id),
        })

    return result


# ── Badge unlock checks ───────────────────────────────────────────────────────

def _check_badge_unlocks(db: Session, user_id: uuid.UUID, triggered_by_event: str) -> None:
    """After awarding points, check if any new badges should be unlocked."""
    from app.models.reward import RewardTransaction, UserBadge
    from app.models.user import User

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return

    # Badges already earned (don't re-award)
    earned = {b.badge_key for b in db.query(UserBadge).filter(UserBadge.user_id == user_id).all()}

    def _grant(key: str):
        if key not in earned:
            badge = UserBadge(id=uuid.uuid4(), user_id=user_id, badge_key=key)
            db.add(badge)
            db.commit()
            earned.add(key)
            logger.info(f"Badge unlocked: user {user_id} earned '{key}'")
            # Send FCM notification for badge
            _notify_badge(user, key)

    try:
        # ── Citizen badge checks ──────────────────────────────────────────────
        if user.role in ("citizen",):
            issue_count = db.query(func.count(RewardTransaction.id)).filter(
                RewardTransaction.user_id == user_id,
                RewardTransaction.event_type == "report_issue",
            ).scalar() or 0

            if issue_count >= 1:
                _grant("first_report")
            if issue_count >= 10:
                _grant("reporter_10")
            if issue_count >= 50:
                _grant("reporter_50")

            # Verified citizen
            if triggered_by_event == "aadhar_verified":
                _grant("verified_citizen")

            # Community voice: total votes_received >= 50
            total_votes = db.query(func.sum(RewardTransaction.points)).filter(
                RewardTransaction.user_id == user_id,
                RewardTransaction.event_type == "vote_received",
            ).scalar() or 0
            if total_votes >= 250:  # 50 votes × 5 pts
                _grant("community_voice")

            # Engaged citizen: rated 10 issues
            rating_count = db.query(func.count(RewardTransaction.id)).filter(
                RewardTransaction.user_id == user_id,
                RewardTransaction.event_type == "rate_issue",
            ).scalar() or 0
            if rating_count >= 10:
                _grant("engaged_citizen")

        # ── Worker badge checks ───────────────────────────────────────────────
        elif user.role == "worker":
            resolve_count = db.query(func.count(RewardTransaction.id)).filter(
                RewardTransaction.user_id == user_id,
                RewardTransaction.event_type == "resolve_issue",
            ).scalar() or 0

            if resolve_count >= 1:
                _grant("first_resolution")
            if resolve_count >= 10:
                _grant("resolver_10")
            if resolve_count >= 100:
                _grant("resolver_100")

            # Fast responder: 10 fast_resolve events
            fast_count = db.query(func.count(RewardTransaction.id)).filter(
                RewardTransaction.user_id == user_id,
                RewardTransaction.event_type == "fast_resolve",
            ).scalar() or 0
            if fast_count >= 10:
                _grant("fast_responder")

            # Top rated: 20 five-star ratings
            five_star_count = db.query(func.count(RewardTransaction.id)).filter(
                RewardTransaction.user_id == user_id,
                RewardTransaction.event_type == "five_star_rating",
            ).scalar() or 0
            if five_star_count >= 20:
                _grant("top_rated")

            # Streak master: 4 weekly_streak events
            streak_count = db.query(func.count(RewardTransaction.id)).filter(
                RewardTransaction.user_id == user_id,
                RewardTransaction.event_type == "weekly_streak",
            ).scalar() or 0
            if streak_count >= 4:
                _grant("streak_master")

    except Exception as e:
        logger.warning(f"Badge check failed (non-fatal): {e}")
        try:
            db.rollback()
        except Exception:
            pass


# ── Weekly streak checker (called from auto-escalation loop or cron) ─────────

def check_weekly_streaks(db: Session) -> int:
    """Award weekly_streak bonuses to workers who had zero rejections this week.

    Should be called once per week (e.g. Sunday midnight) from a background task.

    Returns:
        Number of workers awarded the streak bonus.
    """
    from app.models.issue import Issue
    from app.models.user import User

    one_week_ago = datetime.utcnow() - timedelta(days=7)
    count = 0

    try:
        # Query all recent issues ONCE (optimization: avoid N+1 query pattern)
        recent_active_issues = (
            db.query(Issue)
            .filter(
                Issue.updated_at >= one_week_ago,
                Issue.reassignment_count > 0,
            )
            .all()
        )
        
        workers = get_users_by_role("worker", db)
        for worker in workers:
            # A rejection means the worker's ID appears in an issue's rejected_by_ids
            # updated within the past 7 days.
            rejected_this_week = any(
                str(worker.id) in (issue.rejected_by_ids or [])
                for issue in recent_active_issues
            )
            if not rejected_this_week:
                award_event(db, worker.id, "weekly_streak",
                            note="Zero rejections this week — streak bonus!")
                count += 1
    except Exception as e:
        logger.error(f"Weekly streak check error: {e}", exc_info=True)

    return count


# ── Level system ──────────────────────────────────────────────────────────────

_LEVELS = [
    (0,    1, "Newcomer"),
    (100,  2, "Active"),
    (300,  3, "Contributor"),
    (600,  4, "Champion"),
    (1000, 5, "Hero"),
    (2000, 6, "Legend"),
]


def _points_to_level(points: int) -> tuple[int, str]:
    level, name = 1, "Newcomer"
    for threshold, lvl, lvl_name in _LEVELS:
        if points >= threshold:
            level, name = lvl, lvl_name
    return level, name


def _next_level_threshold(current_level: int) -> Optional[int]:
    for threshold, lvl, _ in _LEVELS:
        if lvl == current_level + 1:
            return threshold
    return None  # already at max level


def _get_rank(db: Session, user_id: uuid.UUID, total_points: int, role: str = "citizen") -> int:
    """Count how many same-role users have more points than this user (1-indexed rank)."""
    from app.models.reward import RewardTransaction
    from app.models.user import User

    try:
        subq = (
            db.query(
                RewardTransaction.user_id,
                func.sum(RewardTransaction.points).label("pts"),
            )
            .join(User, User.id == RewardTransaction.user_id)
            .filter(User.is_active == True, User.role == role)
            .group_by(RewardTransaction.user_id)
            .subquery()
        )
        higher = db.query(func.count()).filter(subq.c.pts > total_points).scalar() or 0
        return higher + 1
    except Exception:
        return 0


def _notify_badge(user, badge_key: str) -> None:
    """Send FCM push for badge unlock (non-fatal)."""
    try:
        if not user.fcm_token:
            return
        from app.services.notification_service import _send_fcm
        badge = BADGE_DEFINITIONS.get(badge_key, {})
        icon = badge.get("icon", "badge")
        name = badge.get("name", badge_key)
        desc = badge.get("description", "")
        _send_fcm(user.fcm_token, f"{icon} Badge Unlocked: {name}", desc)
    except Exception as e:
        logger.warning(f"Badge FCM push failed: {e}")
