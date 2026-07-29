"""
Reward Models — Points Ledger and Badges
=========================================
Two tables:

reward_transactions
    Every point event is logged here (append-only ledger).
    The user's total points = SUM(points) WHERE user_id = X.

user_badges
    Each row means a user has earned a specific badge.
    Badge definitions live in the BADGE_DEFINITIONS dict in rewards_service.py.

Points system:
    Citizens:
        report_issue        +10  per issue created
        issue_resolved      +20  when their reported issue is resolved
        vote_received       +5   per upvote their issue receives
        rate_issue          +5   when they rate a resolved issue
        aadhar_verified     +50  one-time after Aadhaar KYC
        first_report        +25  bonus for very first issue reported

    Workers:
        resolve_issue       +30  per issue resolved
        five_star_rating    +25  bonus when citizen gives 5 stars
        fast_resolve        +15  bonus when resolved within 24 hours of assignment
        weekly_streak       +20  bonus when worker has no rejections for 7 days
        first_resolution    +25  bonus for very first resolved issue
"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class RewardTransaction(Base):
    __tablename__ = "reward_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    points = Column(Integer, nullable=False)                # positive = earned, negative = spent
    event_type = Column(String(50), nullable=False, index=True)
    # event_type values:
    #   report_issue, issue_resolved, vote_received, rate_issue,
    #   aadhar_verified, first_report, resolve_issue, five_star_rating,
    #   fast_resolve, weekly_streak, first_resolution
    reference_id = Column(UUID(as_uuid=True), nullable=True)   # issue/vote/etc UUID
    note = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    __table_args__ = (
        # Backs the ON CONFLICT in rewards_service.award_event. Without a
        # constraint the idempotency guard there was a bare SELECT-then-INSERT,
        # so two concurrent requests both found nothing and both inserted — a
        # double-tapped "resolve" paid twice and inflated the counts that unlock
        # badges. Partial, because reference_id IS NULL means "this event is
        # genuinely repeatable" and must not collide with itself.
        Index(
            "uq_reward_event_reference",
            "user_id", "event_type", "reference_id",
            unique=True,
            postgresql_where=text("reference_id IS NOT NULL"),
        ),
    )

    user = relationship("User")


class UserBadge(Base):
    __tablename__ = "user_badges"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    badge_key = Column(String(50), nullable=False, index=True)  # e.g. "first_report"
    earned_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        # A user holds any given badge at most once. There was no constraint at
        # all — only separate indexes on each column — so the check-then-insert
        # in _check_badge_unlocks could duplicate under concurrency, giving a
        # wrong badge_count on the leaderboard and two "Badge Unlocked" pushes.
        UniqueConstraint("user_id", "badge_key", name="uq_user_badge"),
    )

    user = relationship("User")
