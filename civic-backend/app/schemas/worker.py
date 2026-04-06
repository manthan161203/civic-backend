"""
Worker Schemas
==============
Request/response models for worker task management endpoints.

Frontend Integration Notes:
- Workers must be online (``is_online=true``) to receive task assignments.
- Task flow: assigned → accept → in_progress → resolve (with after-photo).
- Workers can also reject or block tasks with a reason.
"""

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class WorkerStatusUpdate(BaseModel):
    """Request body for ``PUT /workers/status``.

    Toggles the worker's online/offline availability for receiving new tasks.

    Attributes:
        is_online: ``true`` to go online and receive tasks, ``false`` to go offline.
    """

    is_online: bool = Field(..., description="true = available for tasks, false = offline")


class LocationUpdate(BaseModel):
    """Request body for ``PUT /workers/location``.

    Attributes:
        latitude: GPS latitude (-90 to +90).
        longitude: GPS longitude (-180 to +180).
    """

    latitude: float = Field(..., ge=-90, le=90, description="GPS latitude")
    longitude: float = Field(..., ge=-180, le=180, description="GPS longitude")


class TaskAcceptReject(BaseModel):
    """Request body for ``POST /workers/tasks/{id}/reject``.

    Attributes:
        action: Must be ``"reject"`` (accept is a separate endpoint with no body).
        reason: Optional reason for rejection (stored in issue notes).
    """

    action: str = Field(..., description="Must be 'reject'")
    reason: Optional[str] = Field(None, description="Reason for rejecting the task")

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        if v not in ("accept", "reject"):
            raise ValueError("action must be 'accept' or 'reject'")
        return v


class ResolveIssue(BaseModel):
    """Request body for ``POST /workers/tasks/{id}/resolve``.

    Note: The after-photo is uploaded as multipart form data, not JSON.
    ``resolution_notes`` is sent as a form field alongside the photo.

    Attributes:
        resolution_notes: Optional notes about how the issue was resolved.
    """

    resolution_notes: Optional[str] = Field(None, description="Notes about how the issue was resolved")


class WorkerStats(BaseModel):
    """Response model for ``GET /workers/stats``.

    Shows today's task statistics for the current worker.

    Attributes:
        tasks_assigned_today:  Number of tasks assigned to the worker today.
        tasks_completed_today: Number of tasks the worker resolved today.
        tasks_pending:         Number of tasks currently assigned/in_progress.
        avg_rating:            Worker's average citizen rating (null if no ratings yet).
    """

    tasks_assigned_today: int
    tasks_completed_today: int
    tasks_pending: int
    avg_rating: Optional[float] = Field(None, description="Average citizen rating (1-5), null if unrated")
