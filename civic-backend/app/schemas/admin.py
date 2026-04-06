"""
Admin Schemas
=============
Request/response models for admin dashboard and management endpoints.

Frontend Integration Notes:
- All admin endpoints require ``role="admin"`` — other roles get ``403 Forbidden``.
- Worker accounts are created by admins (workers cannot self-register).
- Dashboard stats refresh on every call (no caching).
"""

from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class AssignWorker(BaseModel):
    """Request body for ``POST /admin/issues/{id}/assign`` and ``POST /admin/issues/{id}/reassign``.

    Attributes:
        worker_id: UUID of the worker to assign to the issue. Must be an active worker.
    """

    worker_id: UUID = Field(..., description="UUID of the worker to assign")


class CreateWorker(BaseModel):
    """Request body for ``POST /admin/workers``."""

    phone: str = Field(..., description="Worker's phone number (must be unique)")
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
