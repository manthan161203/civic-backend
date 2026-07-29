"""
Health Check Endpoints
======================

LOW PRIORITY BUG FIX #4: Health monitoring endpoints

Provides system health status endpoints for monitoring and orchestration.
"""

from datetime import datetime, timezone
from typing import Dict, Any

from fastapi import APIRouter, Depends
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.deps import get_db, require_role
from app.core.logger import get_logger
from app.core.rate_limit import limiter
from app.models.user import User

logger = get_logger("health")

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/ready")
@limiter.exempt
async def health_ready(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Readiness probe for Kubernetes/Docker orchestration.
    
    Returns 200 if the service is ready to accept traffic.
    Returns 503 if dependencies are not available (database, etc).
    
    Response:
        {
            "status": "ready" | "not_ready",
            "timestamp": "2026-04-08T10:30:00Z",
            "database": "connected" | "disconnected"
        }
    """
    try:
        # Test database connectivity
        db.execute(text("SELECT 1"))
    except Exception:
        # Must be a 503. This previously returned HTTP 200 with a body saying
        # "not_ready", which every orchestrator reads as ready — the probe was
        # decorative. Note SessionLocal() does not connect eagerly, so the
        # get_db dependency yields fine and the failure only surfaces here.
        return JSONResponse(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "not_ready",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "database": "disconnected",
                "detail": "Database unavailable",
            },
        )

    return {
        "status": "ready",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "database": "connected",
    }


@router.get("/live", response_model=Dict[str, Any])
@limiter.exempt
async def health_live() -> Dict[str, Any]:
    """
    Liveness probe for Kubernetes/Docker orchestration.
    
    Returns 200 if the service is still running.
    Doesn't check dependencies - just confirms the process is alive.
    
    Response:
        {
            "status": "alive",
            "timestamp": "2026-04-08T10:30:00Z"
        }
    """
    return {
        "status": "alive",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@router.get("/integrity", response_model=Dict[str, Any])
async def health_integrity(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_role("admin")),
) -> Dict[str, Any]:
    """
    Detailed integrity check - verifies database consistency. **Roles**: super-admin.

    Not a probe. Unlike ``/health/live`` and ``/health/ready`` this runs four
    full table scans, so it is authenticated and rate-limited: it was previously
    open to the internet with ``@limiter.exempt``, which let anyone saturate the
    database by looping it. It also returned raw ``str(e)`` from SQLAlchemy —
    connection parameters, table names and SQL fragments — to whoever called it.
    Failures are now logged with the detail and reported to the caller as a
    fixed string.

    Runs checks on:
    - Database connection
    - Key table accessibility
    - Orphaned records
    - Soft-delete consistency

    Response:
        {
            "status": "healthy" | "degraded",
            "database": {...},
            "tables": {...},
            "checks": {...},
            "timestamp": "2026-04-08T10:30:00Z"
        }
    """
    checks_passed = 0
    checks_failed = 0
    issues = []
    
    try:
        # Check database connection
        db.execute(text("SELECT 1"))
        db_connected = True
    except Exception as e:
        db_connected = False
        checks_failed += 1
        logger.error("Integrity check: database connection failed: %s", e, exc_info=True)
        issues.append("Database connection failed")
    
    if not db_connected:
        return {
            "status": "unhealthy",
            "database": "disconnected",
            "checks": {
                "passed": checks_passed,
                "failed": checks_failed
            },
            "issues": issues,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    
    checks_passed += 1
    
    # Check table accessibility
    tables_ok = True
    try:
        from app.models import User, Issue
        
        # The queries themselves are the check — they raise if the table is
        # missing or unreadable. The counts are not needed.
        db.query(User).count()
        db.query(Issue).count()
        
        checks_passed += 1
    except Exception as e:
        tables_ok = False
        checks_failed += 1
        logger.error("Integrity check: table accessibility failed: %s", e, exc_info=True)
        issues.append("Table accessibility check failed")
    
    # Check for orphaned issues (assigned to non-existent workers)
    if tables_ok:
        try:
            from app.models import User, Issue
            
            orphaned = db.query(Issue).filter(
                Issue.assigned_worker_id.isnot(None),
                ~db.query(User.id).filter(User.id == Issue.assigned_worker_id).exists()
            ).count()
            
            if orphaned > 0:
                issues.append(f"WARNING: {orphaned} issues assigned to non-existent workers")
                checks_passed += 1
            else:
                checks_passed += 1
                
        except Exception as e:
            checks_failed += 1
            logger.error("Integrity check: orphan check failed: %s", e, exc_info=True)
            issues.append("Orphan check failed")
    
    # Check soft-delete consistency
    if tables_ok:
        try:
            from app.models import Issue
            
            # Issues with is_deleted=True but status not in ("closed", "deleted")
            inconsistent = db.query(Issue).filter(
                Issue.is_deleted == True,
                ~Issue.status.in_(["closed", "deleted"])
            ).count()
            
            if inconsistent > 0:
                issues.append(f"WARNING: {inconsistent} soft-deleted issues with inconsistent status")
                checks_passed += 1
            else:
                checks_passed += 1
                
        except Exception as e:
            checks_failed += 1
            logger.error("Integrity check: soft-delete check failed: %s", e, exc_info=True)
            issues.append("Soft-delete check failed")
    
    status = "healthy" if checks_failed == 0 else ("degraded" if issues else "unhealthy")
    
    return {
        "status": status,
        "database": "connected",
        "tables": tables_ok,
        "checks": {
            "passed": checks_passed,
            "failed": checks_failed
        },
        "issues": issues if issues else None,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@router.get("/", response_model=Dict[str, Any])
@limiter.exempt
async def health_overall(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Overall health status - combines all checks.
    
    Simple endpoint that returns a quick system status.
    
    Response:
        {
            "status": "healthy" | "degraded" | "unhealthy",
            "checks": {
                "ready": true/false,
                "live": true,
                "database": true/false
            }
        }
    """
    try:
        # Quick database check
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    
    status = "healthy" if db_ok else "unhealthy"
    
    return {
        "status": status,
        "checks": {
            "ready": db_ok,
            "live": True,
            "database": db_ok
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
