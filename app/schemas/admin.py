"""
Admin Schemas
=============
Request/response models for admin dashboard and management endpoints.

Frontend Integration Notes:
- All admin endpoints require ``role="admin"`` — other roles get ``403 Forbidden``.
- Worker accounts are created by admins (workers cannot self-register).
- Dashboard stats refresh on every call (no caching).
"""

from typing import List, Optional
from uuid import UUID
from datetime import datetime

from pydantic import BaseModel, Field, validator


class AssignWorker(BaseModel):
    """Request body for ``POST /admin/issues/{id}/assign`` and ``POST /admin/issues/{id}/reassign``.

    Attributes:
        worker_id: UUID of the worker to assign to the issue. Must be an active worker.
    """

    worker_id: UUID = Field(..., description="UUID of the worker to assign")


class CreateWorker(BaseModel):
    """Request body for ``POST /admin/workers``.

    On creation:
    - A temporary password is auto-generated and sent via email
    - The worker account starts as inactive (is_active=False)
    - The worker must change their password within 7 days
    """

    phone: str = Field(..., description="Worker's phone number (must be unique)")
    email: str = Field(..., description="Worker's email address (must be unique, used for invitation)")
    name: Optional[str] = Field(None, description="Worker's display name")
    ward: Optional[str] = Field(None, description="Ward name string (legacy, for display)")
    ward_id: Optional[UUID] = Field(None, description="Structured ward UUID (from /locations)")
    taluka_id: Optional[UUID] = Field(None, description="Taluka UUID (auto-set for ward_admin)")
    district_id: Optional[UUID] = Field(None, description="District UUID")
    department: Optional[str] = Field(None, description="water | roads | electricity | sanitation | parks | other")


class UpdateWorker(BaseModel):
    """Request body for ``PUT /admin/workers/{id}``.

    All fields are optional — only provided fields are updated.
    """

    name: Optional[str] = Field(None, description="New display name")
    phone: Optional[str] = Field(None, description="New phone number (must be unique if changed)")
    ward: Optional[str] = Field(None, description="New ward assignment (legacy)")
    ward_id: Optional[UUID] = Field(None, description="New ward UUID")
    department: Optional[str] = Field(None, description="water | roads | electricity | sanitation | parks | other")
    is_active: Optional[bool] = Field(None, description="true = active, false = deactivated")


class UpdateSubAdmin(BaseModel):
    """Request body for ``PUT /admin/admins/{id}``.

    All fields are optional — only provided fields are updated.
    """

    name: Optional[str] = Field(None, description="New display name")
    phone: Optional[str] = Field(None, description="New phone number (must be unique if changed)")
    role: Optional[str] = Field(None, description="ward_admin | taluka_admin | district_admin")
    ward_id: Optional[UUID] = Field(None, description="New ward UUID")
    taluka_id: Optional[UUID] = Field(None, description="New taluka UUID")
    district_id: Optional[UUID] = Field(None, description="New district UUID")



class DashboardStats(BaseModel):
    """Response model for ``GET /admin/dashboard``.

    Live statistics for the admin dashboard overview.

    Attributes:
        total_open:            Number of issues with status ``"open"`` (unassigned).
        total_in_progress:     Number of issues with status ``"assigned"`` or ``"in_progress"``.
        total_resolved_today:  Number of issues resolved today.
        total_issues:          Total number of issues in the system.
        avg_resolution_hours:  Average time to resolve issues (in hours), null if no resolved issues.
        total_workers_online:  Number of workers currently online and available.
    """

    total_open: int
    total_in_progress: int
    total_resolved_today: int
    total_issues: int
    avg_resolution_hours: Optional[float] = Field(None, description="Average resolution time in hours")
    total_workers_online: int


# ── Geofence Schemas ──────────────────────────────────────────────────────────

class CreateGeofenceRequest(BaseModel):
    """Request body for ``POST /admin/geofences``."""

    name: str = Field(..., min_length=1, max_length=255, description="Zone name")
    latitude: float = Field(..., ge=-90, le=90, description="Latitude (-90 to 90)")
    longitude: float = Field(..., ge=-180, le=180, description="Longitude (-180 to 180)")
    radius_km: float = Field(..., gt=0, description="Radius in kilometers (must be > 0)")

    @validator('name')
    def name_not_blank(cls, v):
        if not v or not v.strip():
            raise ValueError('name cannot be empty or whitespace')
        return v.strip()


class UpdateGeofenceRequest(BaseModel):
    """Request body for ``PATCH /admin/geofences/{id}`` (all fields optional)."""

    name: Optional[str] = Field(None, min_length=1, max_length=255, description="Zone name")
    latitude: Optional[float] = Field(None, ge=-90, le=90, description="Latitude (-90 to 90)")
    longitude: Optional[float] = Field(None, ge=-180, le=180, description="Longitude (-180 to 180)")
    radius_km: Optional[float] = Field(None, gt=0, description="Radius in kilometers (must be > 0)")

    @validator('name')
    def name_not_blank(cls, v):
        if v is not None and (not v or not v.strip()):
            raise ValueError('name cannot be empty or whitespace')
        return v.strip() if v else None


class GeofenceResponse(BaseModel):
    """Response model for geofence endpoints."""

    id: UUID = Field(..., description="Geofence UUID")
    name: str = Field(..., description="Zone name")
    latitude: float = Field(..., description="Latitude")
    longitude: float = Field(..., description="Longitude")
    radius_km: float = Field(..., description="Radius in kilometers")
    created_by_name: Optional[str] = Field(None, description="Name of admin who created this geofence")
    created_at: datetime = Field(..., description="Creation timestamp")

    class Config:
        from_attributes = True


class GeofenceListResponse(BaseModel):
    """Paginated list of geofences.

    Attributes:
        items: List of geofences for the current page.
        total: Total number of geofences.
        page:  Current page number (1-indexed).
        size:  Number of items per page.
    """

    items: List[GeofenceResponse]
    total: int
    page: int
    size: int
