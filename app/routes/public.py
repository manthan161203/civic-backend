"""
Public Routes
=============
Unauthenticated endpoints for public-facing features like the
Ward Health Leaderboard. No token required.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.time import now_utc
from app.core.logger import get_logger
from app.database import get_db
from app.models.issue import Issue

logger = get_logger("public")

router = APIRouter(prefix="/public", tags=["Public"])


@router.get("/leaderboard", response_model=List[Dict])
def ward_health_leaderboard(
    limit: int = Query(25, ge=1, le=100, description="Number of wards to return"),
    days: int = Query(30, ge=1, le=365, description="Score window in days"),
    db: Session = Depends(get_db),
):
    """Public Ward Health Leaderboard.

    Ranks wards by a composite health score based on:
    - Resolution rate  (40 %)
    - Average resolution speed  (30 %)
    - Average citizen rating  (30 %)

    Higher is better. Score range: 0–100.

    Returns:
        Sorted list of ward health entries (rank, ward, score breakdown).
    """
    try:
        cutoff = now_utc() - timedelta(days=days)

        # Aggregate per ward
        rows = (
            db.query(
                Issue.ward,
                func.count(Issue.id).label("total"),
                func.count(Issue.resolved_at).label("resolved"),
                func.avg(
                    func.extract("epoch", Issue.resolved_at) -
                    func.extract("epoch", Issue.created_at)
                ).label("avg_resolve_secs"),
                func.avg(Issue.citizen_rating).label("avg_rating"),
            )
            .filter(
                Issue.ward.isnot(None),
                Issue.ward != "",
                Issue.is_deleted == False,
                Issue.created_at >= cutoff,
            )
            .group_by(Issue.ward)
            .having(func.count(Issue.id) >= 3)  # need at least 3 issues for meaningful score
            .all()
        )

        entries = []
        for row in rows:
            total = row.total or 1
            resolved = row.resolved or 0
            resolution_rate = (resolved / total) * 100

            # Speed score: <24 h = 100, >168 h = 0, linear between
            avg_hrs = (row.avg_resolve_secs or 0) / 3600
            speed_score = max(0, min(100, 100 - ((avg_hrs - 24) / (168 - 24)) * 100)) if avg_hrs > 0 else 50

            # Rating score: maps 1-5 stars → 0-100
            avg_rating = row.avg_rating or 3.0
            rating_score = ((avg_rating - 1) / 4) * 100

            composite = round(
                resolution_rate * 0.40 +
                speed_score * 0.30 +
                rating_score * 0.30,
                1,
            )

            entries.append({
                "ward": row.ward,
                "score": composite,
                "total_issues": total,
                "resolved_issues": resolved,
                "resolution_rate": round(resolution_rate, 1),
                "avg_resolve_hours": round(avg_hrs, 1),
                "speed_score": round(speed_score, 1),
                "avg_rating": round(float(avg_rating), 2),
                "rating_score": round(rating_score, 1),
            })

        # Sort by composite score descending
        entries.sort(key=lambda x: x["score"], reverse=True)
        entries = entries[:limit]

        # Add rank
        for i, entry in enumerate(entries, 1):
            entry["rank"] = i

        logger.info(f"Leaderboard served: {len(entries)} wards, {days}-day window")
        return entries
    except Exception as e:
        logger.error(f"Leaderboard error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to generate leaderboard.")


# ── Public Issue Map (#15) ───────────────────────────────────────────────────


class PublicIssueMarker(BaseModel):
    """Anonymized issue marker for the public map."""
    id: str
    issue_type: str
    custom_issue_type_label: Optional[str] = None
    severity: str
    status: str
    latitude: float
    longitude: float
    ward: Optional[str] = None
    upvote_count: int = 0
    created_at: datetime


@router.get("/issues/map", response_model=List[PublicIssueMarker])
def public_issue_map(
    lat: Optional[float] = Query(None, description="Center latitude for bounding box"),
    lng: Optional[float] = Query(None, description="Center longitude for bounding box"),
    radius_km: float = Query(5.0, le=50.0, description="Search radius in km (default 5, max 50)"),
    issue_type: Optional[str] = Query(None, description="Filter by issue type"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    limit: int = Query(200, ge=1, le=500, description="Max markers to return"),
    db: Session = Depends(get_db),
):
    """Public issue map — returns anonymized issue markers (no auth required).

    Shows active issues on a map for transparency. No personal data is exposed.
    Optionally filter by location (lat/lng + radius), issue_type, or status.

    Returns:
        List of anonymized issue markers with location, type, severity, and status.
    """
    import math

    try:
        query = db.query(Issue).filter(
            Issue.is_deleted == False,
            Issue.status != "closed",
        )

        if lat is not None and lng is not None:
            lat_delta = radius_km / 111.0
            lng_delta = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
            query = query.filter(
                Issue.latitude.between(lat - lat_delta, lat + lat_delta),
                Issue.longitude.between(lng - lng_delta, lng + lng_delta),
            )

        if issue_type:
            query = query.filter(Issue.issue_type == issue_type)
        if status_filter:
            query = query.filter(Issue.status == status_filter)

        issues = query.order_by(Issue.created_at.desc()).limit(limit).all()

        markers = []
        for issue in issues:
            markers.append(PublicIssueMarker(
                id=str(issue.id)[:8],  # short ID only — no full UUID exposed
                issue_type=issue.issue_type,
                custom_issue_type_label=issue.custom_issue_type_label,
                severity=issue.severity,
                status=issue.status,
                latitude=issue.latitude,
                longitude=issue.longitude,
                ward=issue.ward,
                upvote_count=issue.upvote_count,
                created_at=issue.created_at,
            ))

        logger.info(f"Public map served: {len(markers)} markers")
        return markers
    except Exception as e:
        logger.error(f"Public map error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load public issue map.")
