"""reward idempotency constraints and issues.assigned_at

Revision ID: v4w5x6y7z8
Revises: u3v4w5x6y7
Create Date: 2026-07-28 14:20:00.000000

Three changes, all closing gaps where the code *believed* it had a guarantee the
database was not providing.

1. ``uq_reward_event_reference`` — a partial unique index on
   ``(user_id, event_type, reference_id)``. ``rewards_service.award_event`` had
   an idempotency check written as SELECT-then-INSERT with nothing behind it, so
   two concurrent requests both found no existing row and both inserted. A
   double-tapped "resolve" button paid a worker twice and permanently inflated
   the event counts that unlock badges. Partial because ``reference_id IS NULL``
   marks a genuinely repeatable event, which must not collide with itself.

2. ``uq_user_badge`` — unique ``(user_id, badge_key)``. Same class of bug in
   ``_check_badge_unlocks``: both requests computed the same "not yet earned"
   set and both inserted, leaving duplicate badges, an inflated leaderboard
   ``badge_count`` and two push notifications.

3. ``issues.assigned_at`` — when the current worker was assigned. Needed by the
   assignment-reclaim pass (issues left in ``assigned`` that a worker never
   started are otherwise invisible to routing forever) and by the
   ``fast_resolve`` bonus, which measured elapsed time from ``updated_at`` —
   a column ``onupdate=func.now()`` had just set to the same instant as
   ``resolved_at``, so every resolution scored 0.0 hours and won the bonus.

Existing duplicate rows are collapsed before each index is created; without that
the CREATE UNIQUE INDEX fails on any database that has already accumulated them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'v4w5x6y7z8'
down_revision: Union[str, Sequence[str], None] = 'u3v4w5x6y7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── issues.assigned_at ────────────────────────────────────────────────────
    op.add_column(
        "issues",
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_issues_assigned_at", "issues", ["assigned_at"])

    # Backfill: for issues that already have a worker, updated_at is the closest
    # available approximation of when that happened. It is not exact — any later
    # edit moved it — but leaving NULL would make the reclaim pass ignore every
    # pre-existing assignment forever, which is the worse failure.
    op.execute(
        """
        UPDATE issues
        SET assigned_at = updated_at
        WHERE assigned_worker_id IS NOT NULL
          AND assigned_at IS NULL
        """
    )

    # ── reward transaction idempotency ────────────────────────────────────────
    # Collapse pre-existing duplicates, keeping the earliest of each group.
    op.execute(
        """
        DELETE FROM reward_transactions a
        USING reward_transactions b
        WHERE a.reference_id IS NOT NULL
          AND a.user_id    = b.user_id
          AND a.event_type = b.event_type
          AND a.reference_id = b.reference_id
          AND (a.created_at, a.id) > (b.created_at, b.id)
        """
    )
    op.create_index(
        "uq_reward_event_reference",
        "reward_transactions",
        ["user_id", "event_type", "reference_id"],
        unique=True,
        postgresql_where=sa.text("reference_id IS NOT NULL"),
    )

    # ── badge idempotency ─────────────────────────────────────────────────────
    op.execute(
        """
        DELETE FROM user_badges a
        USING user_badges b
        WHERE a.user_id   = b.user_id
          AND a.badge_key = b.badge_key
          AND (a.earned_at, a.id) > (b.earned_at, b.id)
        """
    )
    op.create_unique_constraint(
        "uq_user_badge", "user_badges", ["user_id", "badge_key"]
    )


def downgrade() -> None:
    """Drop the constraints and the column.

    The duplicate rows removed on the way up are not restored — they were
    double-payments and duplicate badges, and reinstating them would be
    reintroducing the corruption, not undoing a change.
    """
    op.drop_constraint("uq_user_badge", "user_badges", type_="unique")
    op.drop_index("uq_reward_event_reference", table_name="reward_transactions")
    op.drop_index("ix_issues_assigned_at", table_name="issues")
    op.drop_column("issues", "assigned_at")
