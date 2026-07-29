"""
Time — a single timezone-aware clock for the whole application
==============================================================

Every ``DateTime`` column in this codebase is declared ``timezone=True``, so
psycopg2 returns **aware** datetimes for them. ``datetime.utcnow()`` returns a
**naive** one. Mixing the two is not a style question — Python raises:

    TypeError: can't subtract offset-naive and offset-aware datetimes

That is exactly what took the background jobs service out. ``_auto_escalate_loop``
computed ``datetime.utcnow() - issue.created_at`` on the first open issue, raised,
and was swallowed by a bare ``except Exception`` — so SLA escalation, weekly
streak rewards, expired-OTP purging, stale-refresh-token purging and the 7-day
worker deactivation had *never once run*, silently, while the container reported
itself alive.

The comparison bugs are worse than the crash, because they do not crash. A naive
datetime bound into a query against a ``timestamptz`` column is interpreted by
Postgres in the **session** timezone. On a UTC session that happens to be right;
on an ``Asia/Kolkata`` session every such comparison is off by 5.5 hours. That
silently broke the 5-second notification de-duplication window and made refresh
token expiry checks depend on undeclared deployment configuration.

So: never call ``datetime.utcnow()``. Call :func:`now_utc`.

For wall-clock comparisons against operator-entered local times — worker shift
strings like ``"09:00"`` are local, not UTC — use :func:`now_local`.
"""

from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger("time")

_local_tz: tzinfo | None = None


def now_utc() -> datetime:
    """Current time as a timezone-aware UTC datetime.

    The replacement for ``datetime.utcnow()`` everywhere in this codebase.

    Returns:
        Aware ``datetime`` in UTC, safe to compare against or store in any
        ``DateTime(timezone=True)`` column.
    """
    return datetime.now(timezone.utc)


def local_tz() -> tzinfo:
    """The deployment's local timezone, from ``settings.LOCAL_TIMEZONE``.

    Cached after the first successful lookup. Falls back to UTC (with a logged
    error) if the configured name is not in the system tz database, rather than
    taking the process down over a display-time setting.
    """
    global _local_tz
    if _local_tz is None:
        try:
            _local_tz = ZoneInfo(settings.LOCAL_TIMEZONE)
        except (ZoneInfoNotFoundError, ValueError):
            logger.error(
                "LOCAL_TIMEZONE=%r is not a valid IANA timezone — falling back to UTC. "
                "Worker shift matching will be wrong unless shifts are entered in UTC.",
                settings.LOCAL_TIMEZONE,
            )
            _local_tz = timezone.utc
    return _local_tz


def now_local() -> datetime:
    """Current time in the deployment's local timezone.

    Use this — never :func:`now_utc` — when comparing against wall-clock values a
    human typed, such as ``WorkerShift.start_time`` / ``end_time``, which are
    ``String(5)`` local times like ``"09:00"``.

    Getting this wrong is not subtle: comparing a UTC ``"%H:%M"`` against IST
    shift strings classified *every* worker with a configured shift as
    off-shift for the entire working day.
    """
    return datetime.now(local_tz())
