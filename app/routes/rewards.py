"""
Rewards Routes — Points, Badges, and Leaderboard
==================================================
Endpoints for the citizen and worker rewards system.

GET  /me/rewards               — current user's points, level, badges, recent history
GET  /leaderboard/citizens     — top citizens ranked by points
GET  /leaderboard/workers      — top workers ranked by points
GET  /badges                   — full list of all available badges (for UI display)
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.core.logger import get_logger
from app.database import get_db
from app.models.user import User
from app.schemas.rewards import BadgeDefinitionOut, LeaderboardEntry, UserRewardSummary
from app.services.rewards_service import BADGE_DEFINITIONS, get_leaderboard, get_user_summary

logger = get_logger("rewards")

router = APIRouter(tags=["Rewards"])


@router.get("/me/rewards", response_model=UserRewardSummary)
def my_rewards(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the current user's reward summary.

    Returns total points, current level, rank among all users of the same role,
    the 10 most recent point transactions, and all earned badges.

    **Roles**: any authenticated user.
    """
    return get_user_summary(db, current_user.id)


#: Time window for a leaderboard, in days. ``None`` is all-time.
#:
#: The board was all-time only, which makes it unwinnable: an account active
#: since launch cannot be caught, so the ranking stops motivating anyone who
#: joins later. Naming matches ``GET /public/leaderboard``, which already took
#: a ``days`` parameter.
_PERIOD_DAYS = Query(
    None,
    ge=1,
    le=365,
    description="Rank on points earned in the last N days. Omit for all-time.",
)


@router.get("/leaderboard/citizens", response_model=List[LeaderboardEntry])
def citizen_leaderboard(
    limit: int = Query(10, ge=1, le=50, description="Number of entries to return"),
    offset: int = Query(0, ge=0, description="Number of entries to skip (for pagination)"),
    days: Optional[int] = _PERIOD_DAYS,
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """Top citizens ranked by total reward points.

    ``level`` and ``badges`` stay lifetime figures even with ``days`` set — a
    badge earned last year is not un-earned by asking for this week's board.

    **Roles**: any authenticated user.
    """
    return get_leaderboard(db, role="citizen", limit=limit, offset=offset, days=days)


@router.get("/leaderboard/workers", response_model=List[LeaderboardEntry])
def worker_leaderboard_rewards(
    limit: int = Query(10, ge=1, le=50, description="Number of entries to return"),
    offset: int = Query(0, ge=0, description="Number of entries to skip (for pagination)"),
    days: Optional[int] = _PERIOD_DAYS,
    db: Session = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """Top workers ranked by total reward points.

    With ``days`` set, ``tasks_completed`` and ``avg_rating`` are restricted to
    the same window (keyed on ``resolved_at``) so the two halves of a row agree.

    **Roles**: any authenticated user.
    """
    return get_leaderboard(db, role="worker", limit=limit, offset=offset, days=days)


@router.get("/badges", response_model=List[BadgeDefinitionOut])
def list_all_badges(
    role: Optional[str] = Query(None, description="Filter by role: citizen | worker"),
    _current_user: User = Depends(get_current_user),
):
    """List all available badges (earned and unearned) for display in the UI.

    Use this to build an achievement gallery showing locked/unlocked states.

    **Roles**: any authenticated user.
    """
    return [
        BadgeDefinitionOut(
            key=key,
            name=defn["name"],
            description=defn["description"],
            icon=defn["icon"],
            role=defn["role"],
        )
        for key, defn in BADGE_DEFINITIONS.items()
        if not role or defn["role"] == role
    ]
