"""
AI Tools — the only database access the chat assistant has
==========================================================

The assistant used to be given LangChain's ``QuerySQLDatabaseTool`` bound to the
application's read-write connection. The model wrote SQL and the tool ran it. The
only thing standing between a citizen and ``SELECT code, phone FROM otps`` was a
paragraph of English in the system prompt asking the model not to.

That is not an access control. A prompt is data, and the user controls data.
"Ignore previous instructions" is a one-line bypass, and the blocklist helpers
that looked like guards (``sql_db_query``, ``sql_db_query_checker``) were never
wired into the agent at all — the agent's tool list was the LangChain built-ins.
Nothing constrained the statement to ``SELECT`` either, so the model could have
been talked into a ``DELETE``.

This module replaces that with tools the model **cannot** misuse, because they do
not take SQL. Each takes typed arguments, runs a fixed ORM query, and applies the
caller's scope from the authenticated ``User`` object — never from an argument
the model supplies. The worst a prompt injection can now achieve is calling a
legitimate tool with unhelpful arguments.

Scope is derived from the same helpers the REST routes use
(:func:`app.core.deps.apply_admin_scope`), so the assistant can never see more
than the same user's dashboard would show.
"""

from typing import Optional

from langchain_core.tools import BaseTool, tool
from sqlalchemy import String, cast, func, or_
from sqlalchemy.orm import Session

from app.core.deps import apply_admin_scope
from app.core.logger import get_logger
from app.core.time import now_utc
from app.models.announcement import Announcement
from app.models.issue import Issue
from app.models.reward import RewardTransaction, UserBadge
from app.models.user import User
from app.models.worker_shift import WorkerShift

logger = get_logger("ai_tools")

# Hard ceiling on rows any tool will return, regardless of what the model asks
# for. Keeps a runaway agent from pulling the table into the prompt.
MAX_ROWS = 25

VALID_STATUSES = ("open", "assigned", "in_progress", "resolved", "closed")


def _scoped_issue_query(db: Session, user: User):
    """Base ``Issue`` query restricted to what this user is allowed to see.

    This is the single chokepoint for the assistant's issue access. Every tool
    that touches ``issues`` starts here.
    """
    q = db.query(Issue).filter(Issue.is_deleted == False)  # noqa: E712

    if user.role == "citizen":
        return q.filter(Issue.reporter_id == user.id)
    if user.role == "worker":
        return q.filter(
            (Issue.assigned_worker_id == user.id) | (Issue.reporter_id == user.id)
        )
    # Admin roles: same geographic scoping the REST endpoints use. For the
    # super-admin this is a no-op; for a scoped admin with no scope assigned it
    # returns nothing (deps.get_admin_scope_filter fails closed).
    return apply_admin_scope(q, user, Issue)


def _fmt_issue(issue: Issue) -> str:
    """One-line summary of an issue. Deliberately excludes reporter identity."""
    return (
        f"- {str(issue.id)[:8]} | {issue.issue_type} | status={issue.status} "
        f"| priority={issue.priority} | ward={issue.ward or 'unknown'} "
        f"| reported {issue.created_at:%d %b %Y}"
        + (f" | resolved {issue.resolved_at:%d %b %Y}" if issue.resolved_at else "")
    )


def build_tools(db: Session, user: User) -> list[BaseTool]:
    """Build the tool set for one request, closed over the caller's identity.

    Args:
        db:   Request-scoped SQLAlchemy session.
        user: The **authenticated** user. Scope comes from here and nowhere else.

    Returns:
        Tools to hand to the agent.
    """

    @tool
    def list_issues(status: Optional[str] = None, limit: int = 10) -> str:
        """List civic issues visible to the current user, newest first.

        Args:
            status: Optionally filter by one of open, assigned, in_progress,
                resolved, closed.
            limit: How many to return (max 25).
        """
        if status and status not in VALID_STATUSES:
            return f"Unknown status {status!r}. Valid: {', '.join(VALID_STATUSES)}."
        q = _scoped_issue_query(db, user)
        if status:
            q = q.filter(Issue.status == status)
        rows = q.order_by(Issue.created_at.desc()).limit(min(limit, MAX_ROWS)).all()
        if not rows:
            return "No matching issues."
        return "\n".join(_fmt_issue(i) for i in rows)

    @tool
    def issue_counts_by_status() -> str:
        """Count the civic issues visible to the current user, grouped by status."""
        sub = _scoped_issue_query(db, user).subquery()
        rows = (
            db.query(sub.c.status, func.count()).group_by(sub.c.status).all()
        )
        if not rows:
            return "No issues in scope."
        return "\n".join(f"{s}: {n}" for s, n in rows)

    @tool
    def issue_counts_by_type() -> str:
        """Count the civic issues visible to the current user, grouped by issue type."""
        sub = _scoped_issue_query(db, user).subquery()
        rows = (
            db.query(sub.c.issue_type, func.count())
            .group_by(sub.c.issue_type)
            .order_by(func.count().desc())
            .all()
        )
        if not rows:
            return "No issues in scope."
        return "\n".join(f"{t}: {n}" for t, n in rows)

    @tool
    def issue_detail(issue_id_prefix: str) -> str:
        """Get the full detail of one issue by its ID or the first 8 characters of it.

        Args:
            issue_id_prefix: Full UUID, or the short 8-character form shown in listings.
        """
        prefix = issue_id_prefix.strip()
        if len(prefix) < 4:
            return "Please give at least the first 4 characters of the issue ID."
        # Prefix match against the text form of the UUID, so the short IDs shown
        # in listings resolve. Still runs inside the scoped query, so this cannot
        # reach an issue the user is not allowed to see.
        issue = (
            _scoped_issue_query(db, user)
            .filter(cast(Issue.id, String).like(f"{prefix}%"))
            .first()
        )
        if not issue:
            return "No such issue, or it is outside what you can access."

        resolved = (
            f"{issue.resolved_at:%d %b %Y %H:%M}" if issue.resolved_at else "not yet"
        )
        escalated = (
            f"yes, level {issue.escalation_level}" if issue.is_escalated else "no"
        )
        return (
            f"ID: {issue.id}\n"
            f"Type: {issue.issue_type}\n"
            f"Status: {issue.status}\n"
            f"Priority: {issue.priority}\n"
            f"Severity: {issue.severity}\n"
            f"Ward: {issue.ward or 'unknown'}\n"
            f"Description: {issue.description}\n"
            f"Escalated: {escalated}\n"
            f"Reported: {issue.created_at:%d %b %Y %H:%M}\n"
            f"Resolved: {resolved}"
        )

    @tool
    def my_rewards() -> str:
        """Get the current user's own reward points total and earned badges."""
        total = (
            db.query(func.coalesce(func.sum(RewardTransaction.points), 0))
            .filter(RewardTransaction.user_id == user.id)
            .scalar()
        )
        badges = (
            db.query(UserBadge.badge_key)
            .filter(UserBadge.user_id == user.id)
            .order_by(UserBadge.earned_at)
            .all()
        )
        badge_list = ", ".join(b[0] for b in badges) if badges else "none yet"
        return f"Points: {total}\nBadges: {badge_list}"

    @tool
    def my_recent_points(limit: int = 10) -> str:
        """List the current user's own recent point transactions.

        Args:
            limit: How many to return (max 25).
        """
        rows = (
            db.query(RewardTransaction)
            .filter(RewardTransaction.user_id == user.id)
            .order_by(RewardTransaction.created_at.desc())
            .limit(min(limit, MAX_ROWS))
            .all()
        )
        if not rows:
            return "No point activity yet."
        return "\n".join(
            f"{t.created_at:%d %b %Y}: {t.points:+d} ({t.event_type})" for t in rows
        )

    @tool
    def my_announcements(limit: int = 5) -> str:
        """List official announcements relevant to the current user, newest first.

        Args:
            limit: How many to return (max 25).
        """
        q = db.query(Announcement).filter(
            (Announcement.expires_at.is_(None)) | (Announcement.expires_at > now_utc())
        )
        # Statewide announcements, plus anything matching the user's own area.
        conditions = [Announcement.scope == "state"]
        if user.ward_id:
            conditions.append(Announcement.ward_id == user.ward_id)
        if user.taluka_id:
            conditions.append(Announcement.taluka_id == user.taluka_id)
        if user.district_id:
            conditions.append(Announcement.district_id == user.district_id)

        rows = (
            q.filter(or_(*conditions))
            .order_by(Announcement.created_at.desc())
            .limit(min(limit, MAX_ROWS))
            .all()
        )
        if not rows:
            return "No current announcements for your area."
        return "\n".join(f"[{a.created_at:%d %b}] {a.title}: {a.body[:200]}" for a in rows)

    @tool
    def my_shifts() -> str:
        """List the current user's own work shifts. Only meaningful for workers."""
        if user.role != "worker":
            return "Shifts apply to field workers only."
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        rows = (
            db.query(WorkerShift)
            .filter(WorkerShift.worker_id == user.id, WorkerShift.is_active == True)  # noqa: E712
            .order_by(WorkerShift.day_of_week)
            .all()
        )
        if not rows:
            return "No shifts configured."
        return "\n".join(
            f"{days[s.day_of_week]}: {s.start_time}–{s.end_time}" for s in rows
        )

    @tool
    def team_workload() -> str:
        """Show how many active tasks each worker in your jurisdiction is carrying.

        Administrators only.
        """
        if user.role not in ("ward_admin", "taluka_admin", "district_admin", "admin"):
            return "That information is available to administrators only."
        scoped = _scoped_issue_query(db, user).subquery()
        rows = (
            db.query(User.name, func.count(scoped.c.id))
            .select_from(User)
            .outerjoin(
                scoped,
                (scoped.c.assigned_worker_id == User.id)
                & (scoped.c.status.in_(["assigned", "in_progress"])),
            )
            .filter(User.role == "worker", User.is_active == True)  # noqa: E712
            .group_by(User.id, User.name)
            .order_by(func.count(scoped.c.id).desc())
            .limit(MAX_ROWS)
            .all()
        )
        if not rows:
            return "No active workers in your jurisdiction."
        return "\n".join(f"{name or 'unnamed'}: {n} active task(s)" for name, n in rows)

    return [
        list_issues,
        issue_counts_by_status,
        issue_counts_by_type,
        issue_detail,
        my_rewards,
        my_recent_points,
        my_announcements,
        my_shifts,
        team_workload,
    ]
