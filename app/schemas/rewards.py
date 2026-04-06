"""
Reward Schemas — Points, Badges, and Leaderboard
=================================================
Pydantic response models for the rewards system.
These are used by /me/rewards, /leaderboard/*, and /badges endpoints.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class RewardTransactionOut(BaseModel):
    """A single entry in a user's point ledger."""
    id: str
    points: int
    event_type: str
    note: Optional[str] = None
    earned_at: str  # ISO 8601 string


class BadgeOut(BaseModel):
    """A badge earned by a user (includes earn timestamp)."""
    key: str
    name: str
    description: str
    icon: str
    earned_at: str  # ISO 8601 string


class BadgeDefinitionOut(BaseModel):
    """A badge definition for the badge gallery (no earn timestamp)."""
    key: str
    name: str
    description: str
    icon: str
    role: str  # "citizen" | "worker"


class UserRewardSummary(BaseModel):
    """Full reward profile for the current user.

    Returned by ``GET /me/rewards``.
    """
    total_points: int
    rank: int
    level: int
    level_name: str
    next_level_points: Optional[int] = None   # None when at max level
    recent_transactions: List[RewardTransactionOut]
    badges: List[BadgeOut]


class LeaderboardEntry(BaseModel):
    """Single row in a leaderboard response.

    Returned by ``GET /leaderboard/citizens`` and ``GET /leaderboard/workers``.
    """
    rank: int
    user_id: str
    name: str
    ward: Optional[str] = None
    total_points: int
    level: int
    level_name: str
    badge_count: int
    tasks_completed: int = 0
    avg_rating: Optional[float] = None
