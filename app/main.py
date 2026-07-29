"""
Application Entry Point — FastAPI app factory and startup configuration
=======================================================================
Sets up:
  - CORS middleware (configurable via CORS_ORIGINS env var)
  - Global exception handlers (HTTP errors, validation errors)
  - Auto-escalation background task (runs every hour):
      * Escalates issues open > 48 h with no assignment
      * Awards weekly_streak bonuses to workers every Sunday
      * Purges expired OTPs and stale revoked refresh tokens
  - On startup: DB health check, dev-mode static file serving for uploads
  - All route routers (auth, issues, workers, admin, features, locations, rewards, chat, notifications)
"""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

from fastapi import FastAPI, Request
from sqlalchemy import text
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware


# FIX MEDIUM PRIORITY BUG #6: Request size limit middleware
class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Middleware to limit request body size.
    
    Prevents DoS attacks via large payloads.
    Default limit: 10 MB
    """
    def __init__(self, app, max_body_size: int = 10 * 1024 * 1024):  # 10 MB default
        super().__init__(app)
        self.max_body_size = max_body_size
    
    async def dispatch(self, request: Request, call_next):
        if request.method in ["POST", "PUT", "PATCH"]:
            if "content-length" in request.headers:
                content_length = int(request.headers["content-length"])
                if content_length > self.max_body_size:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": f"Request body too large (max {self.max_body_size // (1024*1024)} MB)"}
                    )
        return await call_next(request)

from app.core.time import now_utc
from app.core.config import settings, validate_settings
from app.core.exceptions import CivicException
from app.core.logger import get_logger
from app.core.rate_limit import limiter
from app.core.request_context import set_request_id
from app.database import SessionLocal, check_db_connection, dispose_pool
from app.routes import admin, auth, chat, citizen_features, complaints, features, health, issues, locations, notifications, public, rewards, setup, sync, workers
from app.services.pool_monitor import PoolHealthCheckThread
from app.services.utils import get_users_with_roles

logger = get_logger("main")

# Initialize Sentry error tracking. An empty DSN disables the client entirely,
# so this is safe when Sentry is not configured.
sentry_sdk.init(
    dsn=settings.SENTRY_DSN,
    integrations=[
        FastApiIntegration(),
        SqlalchemyIntegration(),
        LoggingIntegration(
            level=logging.INFO,        # Capture info and above
            event_level=logging.ERROR  # Send errors to Sentry
        ),
    ],
    # Performance Monitoring (sample 10% of transactions)
    traces_sample_rate=0.1,
    # Capture release information
    release=settings.APP_VERSION,
    # Environment flag
    environment=settings.ENVIRONMENT,
    # Custom user context
    attach_stacktrace=True,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — see :func:`_startup` and :func:`_shutdown`."""
    await _startup(app)
    try:
        yield
    finally:
        await _shutdown()


app = FastAPI(
    title="Civic Issue API",
    version=settings.APP_VERSION,
    docs_url="/docs" if settings.DOCS_ENABLED else None,
    redoc_url="/redoc" if settings.DOCS_ENABLED else None,
    # Previously left at its default, so the complete API schema stayed
    # publicly readable even with /docs and /redoc disabled.
    openapi_url="/openapi.json" if settings.DOCS_ENABLED else None,
    lifespan=lifespan,
)
app.state.limiter = limiter

_bg_task: asyncio.Task | None = None   # reference to the background escalation task
_pool_monitor_thread: PoolHealthCheckThread | None = None  # reference to pool health check thread

_origins = settings.cors_origin_list

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=_origins != ["*"],
)

# MEDIUM PRIORITY BUG FIX #6: Add request size limit middleware
app.add_middleware(RequestSizeLimitMiddleware, max_body_size=settings.MAX_BODY_SIZE)

# Without this, `default_limits` on the Limiter above was never enforced and
# every route outside app/routes/auth.py was unthrottled.
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(_request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests. Please slow down."},
    )


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Attach a unique request ID, log method/path/status/duration, add security headers."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    # Bind it for the whole request, so every logger below picks it up.
    set_request_id(request_id)
    start = time.time()
    response = await call_next(request)
    duration = round((time.time() - start) * 1000)
    logger.info(f"{request.method} {request.url.path} → {response.status_code} ({duration}ms)")

    # Request tracing header
    response.headers["X-Request-ID"] = request_id

    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"

    return response


@app.exception_handler(CivicException)
async def civic_exception_handler(request: Request, exc: CivicException):
    """Map the application's exception hierarchy onto HTTP responses.

    Every CivicException already carries the status code it means (503 for a
    transient storage failure, 404 for a missing resource, ...). Without this
    handler none of that was read: the exceptions escaped as unhandled errors
    and every one of them became a generic 500.
    """
    log = logger.warning if exc.status_code < 500 else logger.error
    log(
        "%s on %s %s: %s",
        exc.__class__.__name__,
        request.method,
        request.url.path,
        exc.message,
        exc_info=exc.status_code >= 500,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message, **exc.to_dict()},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError):
    errors = []
    for err in exc.errors():
        err_copy = {k: v for k, v in err.items() if k != "ctx"}
        if "ctx" in err:
            err_copy["ctx"] = {k: str(v) for k, v in err["ctx"].items()}
        errors.append(err_copy)
    return JSONResponse(
        status_code=422,
        content={"detail": errors, "message": "Validation error"},
    )


# Arbitrary but fixed application-level lock id for the escalation job. Any
# process running the job takes this lock; only one can hold it at a time.
_ESCALATION_LOCK_KEY = 8_471_002_931


SLA_THRESHOLDS = {
    "urgent": 24,
    "high": 48,
    "medium": 72,
    "low": 168,
}


def run_escalation_pass(db, now=None) -> int:
    """Escalate every overdue open issue and notify the right admin tier.

    Extracted from the hourly loop so it can be called directly — by a test, or
    by an operator wanting to force a pass. The loop below is now only a timer.

    Args:
        db:  SQLAlchemy session.
        now: Override for the current time; defaults to :func:`now_utc`.

    Returns:
        Number of issues escalated.
    """
    from app.models.issue import Issue
    from app.models.location import Ward as WardModel
    from app.services.notification_service import notify_localized

    now = now or now_utc()
    escalated_count = 0

    open_issues = (
        db.query(Issue)
        .filter(Issue.status.in_(["open", "assigned", "in_progress"]))
        .all()
    )

    for issue in open_issues:
        sla_hours = SLA_THRESHOLDS.get(issue.priority, 72)
        age_hours = (now - issue.created_at).total_seconds() / 3600
        current_level = issue.escalation_level or 0

        # Determine target escalation level based on SLA multiplier
        if age_hours >= sla_hours * 3 and current_level < 3:
            target_level = 3
        elif age_hours >= sla_hours * 2 and current_level < 2:
            target_level = 2
        elif age_hours >= sla_hours and current_level < 1:
            target_level = 1
        else:
            continue  # No escalation needed

        issue.is_escalated = True
        issue.escalated_at = now
        issue.escalation_level = target_level
        escalated_count += 1

        hours_overdue = round(age_hours - sla_hours, 1)
        tpl_kwargs = dict(
            issue_id_short=str(issue.id)[:8],
            issue_type=issue.issue_type,
            ward=issue.ward or "unknown",
            sla_hours=sla_hours,
            hours_overdue=hours_overdue,
        )

        # Level 1 -> ward admins
        if target_level == 1:
            key = "escalation_ward"
            target_admins = get_users_with_roles(["ward_admin", "admin"], db)
            if issue.ward_id:
                # Filter to only ward-specific admins or global admins
                target_admins = [a for a in target_admins if a.ward_id == issue.ward_id or a.role == "admin"]

        # Level 2 -> taluka admins
        elif target_level == 2:
            key = "escalation_taluka"
            target_admins = []
            if issue.ward_id:
                ward_obj = db.query(WardModel).filter(WardModel.id == issue.ward_id).first()
                if ward_obj and ward_obj.taluka_id:
                    all_taluka_admins = get_users_with_roles(["taluka_admin", "admin"], db)
                    target_admins = [a for a in all_taluka_admins if a.taluka_id == ward_obj.taluka_id or a.role == "admin"]
            if not target_admins:
                target_admins = get_users_with_roles(["taluka_admin", "admin"], db)

        # Level 3 -> district admins + super admins
        else:
            key = "escalation_district"
            target_admins = get_users_with_roles(["district_admin", "admin"], db)

        for admin_user in target_admins:
            notify_localized(
                db=db, user=admin_user, key=key,
                notification_type="system",
                issue_id=str(issue.id),
                action_type="open_issue",
                **tpl_kwargs,
            )

        logger.warning(
            f"ESCALATION L{target_level}: Issue {str(issue.id)[:8]} "
            f"({issue.priority.upper()}, SLA={sla_hours}h) "
            f"overdue by {hours_overdue}h"
        )

    if escalated_count > 0:
        db.commit()
        logger.info(f"Tiered escalation: {escalated_count} issue(s) escalated")

    return escalated_count


def run_assignment_reclaim_pass(db, timeout_minutes: int = 60, now=None) -> int:
    """Reclaim assignments a worker never started, so the issue is not stranded.

    An issue sitting in ``assigned`` long after the worker was given it means the
    worker is not going to act on it. Left alone it stays invisible to routing
    forever: it is not ``open``, so auto-assignment skips it, and it is not
    ``in_progress``, so nobody is working it. It just ages until the escalation
    pass starts paging admins about an issue no one ever picked up.

    Reclaiming sets it back to ``open`` and records the worker in
    ``rejected_by_ids`` so the router does not immediately hand it back to the
    same person.

    This is the feature ``app/services/worker_utils.py`` claimed to implement. It
    could not: it filtered on ``Issue.status == "queued"`` and ``Issue.queued_at``,
    neither of which exists, so the ``AttributeError`` was caught and the function
    returned ``{"processed": 0, "failed": 0}`` — a clean success metric for a
    subsystem that had never run. The module was unreferenced and is now deleted.

    Args:
        db:              SQLAlchemy session.
        timeout_minutes: How long an untouched assignment may sit before reclaim.
        now:             Override for the current time.

    Returns:
        Number of issues reclaimed.
    """
    from app.models.issue import Issue

    now = now or now_utc()
    cutoff = now - timedelta(minutes=timeout_minutes)

    stale = (
        db.query(Issue)
        .filter(
            Issue.status == "assigned",
            Issue.assigned_worker_id.isnot(None),
            Issue.assigned_at.isnot(None),
            Issue.assigned_at < cutoff,
            Issue.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    if not stale:
        return 0

    for issue in stale:
        previous_worker_id = str(issue.assigned_worker_id)

        # Remember the refusal so routing does not hand it straight back.
        rejected = list(issue.rejected_by_ids or [])
        if previous_worker_id not in rejected:
            rejected.append(previous_worker_id)
        issue.rejected_by_ids = rejected

        issue.assigned_worker_id = None  # listener clears assigned_at
        issue.status = "open"
        issue.reassignment_count = (issue.reassignment_count or 0) + 1

        logger.warning(
            "Reclaimed issue %s from worker %s after %d minutes unstarted",
            str(issue.id)[:8],
            previous_worker_id[:8],
            timeout_minutes,
        )

    db.commit()
    logger.info("Assignment reclaim: %d issue(s) returned to the open pool", len(stale))
    return len(stale)


def run_announcement_push_pass(db, batch_size: int = 500) -> int:
    """Deliver notifications for announcements that have not been pushed yet.

    Runs in the ``jobs`` service. The POST handler that creates an announcement
    only records it and returns; the fan-out happens here, where taking a while
    is fine and a failure can be retried on the next cycle instead of being
    swallowed behind a 201.

    Args:
        db:         SQLAlchemy session.
        batch_size: Maximum recipients to notify per announcement per cycle.

    Returns:
        Number of announcements dispatched.
    """
    from app.models.announcement import Announcement
    from app.models.user import User
    from app.services.notification_service import notify

    pending = (
        db.query(Announcement)
        .filter(Announcement.push_dispatched_at.is_(None))
        .order_by(Announcement.created_at)
        .limit(20)
        .all()
    )
    if not pending:
        return 0

    dispatched = 0
    for ann in pending:
        query = db.query(User).filter(
            User.role == "citizen",
            User.is_active == True,  # noqa: E712
        )
        if ann.scope == "ward" and ann.ward_id:
            query = query.filter(User.ward_id == ann.ward_id)
        elif ann.scope == "taluka" and ann.taluka_id:
            query = query.filter(User.taluka_id == ann.taluka_id)
        elif ann.scope == "district" and ann.district_id:
            query = query.filter(User.district_id == ann.district_id)
        # scope == "state" → every citizen

        sent = 0
        try:
            for citizen in query.limit(batch_size).all():
                notify(
                    db=db,
                    user_id=str(citizen.id),
                    title=ann.title,
                    body=ann.body,
                    notification_type="system",
                    fcm_token=citizen.fcm_token,
                    location_lat=ann.location_lat,
                    location_lng=ann.location_lng,
                )
                sent += 1
        except Exception as e:
            # Leave push_dispatched_at NULL so the next cycle retries it. This
            # is the behaviour the inline version could not offer: it logged a
            # warning and the announcement was never sent to anyone again.
            db.rollback()
            logger.error(
                "Announcement %s push failed after %d recipient(s): %s",
                ann.id, sent, e, exc_info=True,
            )
            continue

        ann.push_dispatched_at = now_utc()
        db.commit()
        dispatched += 1
        logger.info("Announcement %s pushed to %d citizen(s)", ann.id, sent)

    return dispatched


def run_cleanup_pass(db, now=None) -> dict:
    """Purge expired OTPs and stale refresh tokens; retire lapsed invitations.

    Args:
        db:  SQLAlchemy session.
        now: Override for the current time; defaults to :func:`now_utc`.

    Returns:
        Counts of what was removed, for logging and assertions.
    """
    from app.models.otp import OTP
    from app.models.refresh_token import RefreshToken
    from app.models.user import User

    now = now or now_utc()

    deleted_otps = db.query(OTP).filter(OTP.expires_at < now).delete(synchronize_session=False)
    stale_cutoff = now - timedelta(days=30)
    deleted_tokens = (
        db.query(RefreshToken)
        .filter(
            (RefreshToken.expires_at < now) | (RefreshToken.is_revoked == True),
            RefreshToken.created_at < stale_cutoff,
        )
        .delete(synchronize_session=False)
    )
    if deleted_otps or deleted_tokens:
        db.commit()
        logger.info(f"Cleanup: removed {deleted_otps} expired OTPs, {deleted_tokens} stale tokens")

    # 7-day worker deactivation: pending workers who never changed password
    deadline = now - timedelta(days=7)
    expired_workers = (
        db.query(User)
        .filter(
            User.role == "worker",
            User.is_active == False,
            User.must_change_password == True,
            User.invitation_sent_at != None,
            User.invitation_sent_at < deadline,
        )
        .all()
    )
    if expired_workers:
        for w in expired_workers:
            w.invitation_sent_at = None  # Clear deadline so it doesn't re-trigger
        db.commit()
        logger.info(f"Auto-deactivated {len(expired_workers)} worker(s) past 7-day invitation window")

    return {
        "otps": deleted_otps,
        "tokens": deleted_tokens,
        "lapsed_invitations": len(expired_workers),
    }


def run_maintenance_cycle(db, last_streak_date=None):
    """Run one full cycle: escalation, weekly streaks, cleanup.

    Args:
        db:               SQLAlchemy session.
        last_streak_date: Date the streak bonus was last granted, so it runs once
                          per Sunday.

    Returns:
        The (possibly updated) ``last_streak_date``.
    """
    # Reclaim before escalating, so an issue whose worker never started it goes
    # back to the open pool in the same cycle rather than escalating to an admin
    # about a worker who was never going to act.
    run_assignment_reclaim_pass(db, timeout_minutes=settings.ASSIGNMENT_TIMEOUT_MINUTES)
    run_escalation_pass(db)
    run_announcement_push_pass(db)

    # Weekly streak check - run once per week (Sunday)
    today = now_utc().date()
    if today.weekday() == 6 and today != last_streak_date:
        from app.services.rewards_service import check_weekly_streaks
        awarded = check_weekly_streaks(db)
        last_streak_date = today
        logger.info(f"Weekly streak check: awarded streak bonus to {awarded} worker(s)")

    run_cleanup_pass(db)
    return last_streak_date


async def _auto_escalate_loop():
    """Background task: every hour, run one :func:`run_maintenance_cycle`.

    Tiered escalation chain:
      - Level 1 (1x SLA breach): Escalate to ward_admin
      - Level 2 (2x SLA breach): Escalate to taluka_admin
      - Level 3 (3x SLA breach): Escalate to district_admin + super-admin

    SLA thresholds by priority: urgent 24h, high 48h, medium 72h, low 168h.
    """
    last_streak_date = None

    while True:
        await asyncio.sleep(3600)
        db = None
        holds_lock = False
        try:
            db = SessionLocal()

            # Belt and braces on top of RUN_BACKGROUND_JOBS: a session-level
            # advisory lock means that even if two processes are configured
            # to run jobs, only one executes a cycle. Without it a second
            # runner re-escalates the same issues, sends duplicate
            # notifications, and grants the weekly streak bonus twice -
            # which is data corruption, not just noise.
            holds_lock = bool(
                db.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": _ESCALATION_LOCK_KEY},
                ).scalar()
            )
            if not holds_lock:
                logger.info("Auto-escalation skipped - another process holds the job lock")
                continue

            last_streak_date = run_maintenance_cycle(db, last_streak_date)

        except Exception as e:
            # exc_info is not optional here. Without it this handler printed a
            # single line - "can't subtract offset-naive and offset-aware
            # datetimes" - with no traceback and no indication that it had
            # aborted the entire cycle, so escalation, streak rewards, OTP
            # purging and token purging silently did nothing for as long as
            # the service ran.
            logger.error(f"Auto-escalation cycle failed: {e}", exc_info=True)
            if db is not None:
                db.rollback()
        finally:
            if db is not None:
                if holds_lock:
                    # Release before closing; the lock is session-scoped and
                    # would otherwise be held until the connection is recycled.
                    try:
                        db.execute(
                            text("SELECT pg_advisory_unlock(:key)"),
                            {"key": _ESCALATION_LOCK_KEY},
                        )
                        db.commit()
                    except Exception as unlock_err:
                        logger.warning(f"Could not release escalation lock: {unlock_err}")
                db.close()


async def _startup(app: FastAPI) -> None:
    """Validate configuration, mount static uploads, start background workers."""
    global _bg_task, _pool_monitor_thread

    # First, before anything else touches the database or the network: a
    # production process with an unsafe configuration must not start at all.
    validate_settings()

    logger.info(
        "Civic API starting (environment=%s, version=%s)",
        settings.ENVIRONMENT,
        settings.APP_VERSION,
    )
    if settings.mocked_backends:
        logger.warning(
            "Mocked integrations active: %s — nothing is actually sent",
            ", ".join(settings.mocked_backends),
        )

    # Check database connection with retries
    if not check_db_connection():
        logger.critical("DATABASE IS UNREACHABLE — the API will return 503 on DB-dependent routes")

    # Serve locally stored photos. Gated on the storage backend, not on the
    # environment: it is the backend that decides whether these files exist on
    # disk. Gating this on DEV_MODE meant uploads succeeded and returned URLs
    # that 404'd for every client as soon as dev mode was turned off.
    if settings.STORAGE_BACKEND == "local":
        uploads_dir = settings.uploads_path
        uploads_dir.mkdir(parents=True, exist_ok=True)
        app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")
        logger.info("Serving local uploads from %s", uploads_dir)

    if settings.RUN_BACKGROUND_JOBS:
        _bg_task = asyncio.create_task(_auto_escalate_loop())
        logger.info("Auto-escalation background task started (runs every hour)")

        _pool_monitor_thread = PoolHealthCheckThread(interval_seconds=30, daemon=True)
        _pool_monitor_thread.start()
        logger.info("Connection pool health monitoring started (interval: 30s)")
    else:
        logger.info("RUN_BACKGROUND_JOBS is false — escalation and pool monitor not started")


async def _shutdown() -> None:
    """Cancel background workers and close pooled database connections."""
    global _bg_task, _pool_monitor_thread
    logger.info("Civic API shutting down — cancelling background tasks")

    # Stop auto-escalation task
    if _bg_task and not _bg_task.done():
        _bg_task.cancel()
        try:
            await _bg_task
        except asyncio.CancelledError:
            pass

    # Stop pool monitoring thread
    if _pool_monitor_thread and _pool_monitor_thread.is_alive():
        _pool_monitor_thread.stop()
        _pool_monitor_thread.join(timeout=5)
        logger.info("Connection pool monitoring stopped")

    # Close all pooled DB connections
    dispose_pool()
    logger.info("Civic API shutdown complete")


@app.get("/")
@limiter.exempt
def health_check():
    """Health check — pings the database to confirm connectivity."""
    from sqlalchemy import text
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "message": "Civic API running", "database": "connected"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "error", "database": "disconnected"})
    finally:
        db.close()


@app.get("/health/pool")
def pool_health_check():
    """Detailed connection pool health check — includes statistics and recommendations.

    Returns detailed pool statistics and health assessment. Useful for:
    - Monitoring connection pool usage
    - Debugging connection issues
    - Capacity planning

    Gated on POOL_HEALTH_ENDPOINT_ENABLED, which defaults off in production.
    """
    if not settings.POOL_HEALTH_ENDPOINT_ENABLED:
        return JSONResponse(status_code=404, content={"detail": "Not found"})

    from app.services.pool_monitor import get_pool_health
    return get_pool_health()


# Routers
app.include_router(health.router)  # Health check endpoints (should be first)
app.include_router(setup.router)
app.include_router(auth.router)
app.include_router(features.router)   # must be before issues — /issues/search must not be shadowed by /issues/{issue_id}
app.include_router(citizen_features.router)  # disputes, surveys, bookmarks, custom types
app.include_router(issues.router)
app.include_router(workers.router)
app.include_router(admin.router)
app.include_router(complaints.router)  # worker complaints
app.include_router(notifications.router)
app.include_router(chat.router)
app.include_router(locations.router)
app.include_router(rewards.router)
app.include_router(sync.router)  # offline sync
# NOTE: app/routes/optimization.py was removed. Its six endpoints were registered
# with no prefix and no role check, so `POST /tasks/cleanup-resolved` (a hard
# DELETE of resolved issues) and `POST /bulk/mark-resolved` were reachable by any
# authenticated citizen. Three of the six also referenced columns that do not
# exist (`Issue.title`, `Issue.assigned_to`, `assigned_to_id`) and had never run.
# The working functionality is duplicated by scoped equivalents in admin.py.
app.include_router(public.router)
