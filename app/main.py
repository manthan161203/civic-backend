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
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address


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

from app.core.config import settings
from app.core.logger import get_logger
from app.database import SessionLocal, check_db_connection, engine, dispose_pool
from app.routes import admin, auth, chat, citizen_features, complaints, features, health, issues, locations, notifications, optimization, public, rewards, setup, sync, workers
from app.services.pool_monitor import PoolHealthCheckThread
from app.services.utils import get_users_by_role, get_users_with_roles

logger = get_logger("main")

# Initialize Sentry error tracking
sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN", ""),  # Set via environment variable
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
    release=os.getenv("APP_VERSION", "0.1.0"),
    # Environment flag
    environment=os.getenv("ENVIRONMENT", "development"),
    # Custom user context
    attach_stacktrace=True,
)

# Global rate limiter  — keyed by client IP
limiter = Limiter(key_func=get_remote_address, default_limits=[f"{settings.RATE_LIMIT_PER_MINUTE}/minute"])

app = FastAPI(
    title="Civic Issue API",
    version="1.0.0",
    docs_url="/docs" if settings.DEV_MODE else None,
    redoc_url="/redoc" if settings.DEV_MODE else None,
)
app.state.limiter = limiter

_bg_task: asyncio.Task | None = None   # reference to the background escalation task
_pool_monitor_thread: PoolHealthCheckThread | None = None  # reference to pool health check thread

_origins = (
    ["*"] if settings.CORS_ORIGINS.strip() == "*"
    else [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=_origins != ["*"],
)

# MEDIUM PRIORITY BUG FIX #6: Add request size limit middleware
max_body_size = int(os.getenv("MAX_BODY_SIZE", 10 * 1024 * 1024))  # 10 MB default
app.add_middleware(RequestSizeLimitMiddleware, max_body_size=max_body_size)


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
    start = time.time()
    response = await call_next(request)
    duration = round((time.time() - start) * 1000)
    logger.info(f"[{request_id}] {request.method} {request.url.path} → {response.status_code} ({duration}ms)")

    # Request tracing header
    response.headers["X-Request-ID"] = request_id

    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if not settings.DEV_MODE:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"

    return response


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


async def _auto_escalate_loop():
    """Background task: every hour, run tiered SLA escalation.

    Tiered escalation chain (#5):
      - Level 1 (1x SLA breach): Escalate to ward_admin
      - Level 2 (2x SLA breach): Escalate to taluka_admin
      - Level 3 (3x SLA breach): Escalate to district_admin + super-admin

    SLA thresholds by priority:
      - Urgent: 24 hours
      - High: 48 hours
      - Medium: 72 hours (3 days)
      - Low: 168 hours (7 days)

    Also runs weekly streak check every Sunday at midnight (UTC).
    """
    last_streak_date = None

    SLA_THRESHOLDS = {
        "urgent": 24,
        "high": 48,
        "medium": 72,
        "low": 168,
    }

    while True:
        await asyncio.sleep(3600)
        try:
            from app.models.issue import Issue
            from app.models.user import User
            from app.models.location import Ward as WardModel, Taluka
            from app.services.notification_service import notify_localized

            db = SessionLocal()
            try:
                now = datetime.utcnow()
                escalated_count = 0

                # Get all unresolved issues (including already-escalated ones for tiered re-escalation)
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

                    # Level 1 → ward admins
                    if target_level == 1:
                        key = "escalation_ward"
                        target_admins = get_users_with_roles(["ward_admin", "admin"], db)
                        if issue.ward_id:
                            # Filter to only ward-specific admins or global admins
                            target_admins = [a for a in target_admins if a.ward_id == issue.ward_id or a.role == "admin"]

                    # Level 2 → taluka admins
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

                    # Level 3 → district admins + super admins
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

                # Weekly streak check — run once per week (Sunday)
                today = datetime.utcnow().date()
                if today.weekday() == 6 and today != last_streak_date:
                    from app.services.rewards_service import check_weekly_streaks
                    awarded = check_weekly_streaks(db)
                    last_streak_date = today
                    logger.info(f"Weekly streak check: awarded streak bonus to {awarded} worker(s)")

                # Expired OTP / token cleanup
                from app.models.otp import OTP
                from app.models.refresh_token import RefreshToken
                now = datetime.utcnow()
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
                from app.models.user import User

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

            finally:
                db.close()
        except Exception as e:
            logger.error(f"Auto-escalation error: {e}")


@app.on_event("startup")
async def startup():
    global _bg_task, _pool_monitor_thread
    logger.info("Civic API started successfully")

    # Check database connection with retries
    if not check_db_connection():
        logger.critical("DATABASE IS UNREACHABLE — the API will return 503 on DB-dependent routes")

    # Set up local file serving in dev mode
    if settings.DEV_MODE:
        uploads_dir = Path(__file__).resolve().parents[1] / "uploads"
        uploads_dir.mkdir(exist_ok=True)
        app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")
        logger.info(f"[DEV] Serving local uploads from {uploads_dir}")

    # Start background escalation task
    _bg_task = asyncio.create_task(_auto_escalate_loop())
    logger.info("Auto-escalation background task started (runs every hour)")

    # Start connection pool health monitoring thread
    _pool_monitor_thread = PoolHealthCheckThread(interval_seconds=30, daemon=True)
    _pool_monitor_thread.start()
    logger.info("Connection pool health monitoring started (interval: 30s)")


@app.on_event("shutdown")
async def shutdown():
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

    Only available in DEV_MODE for security.
    """
    if not settings.DEV_MODE:
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
app.include_router(optimization.router)  # query optimization & performance
app.include_router(public.router)
