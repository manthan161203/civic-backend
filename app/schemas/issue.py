"""
Issue Schemas
=============
Request/response models for civic issue endpoints.

Frontend Integration Notes:
- Issue types: ``garbage``, ``pothole``, ``streetlight``, ``drain``, ``other``
- Severity levels: ``high``, ``medium``, ``low``
- Status flow: ``open`` → ``assigned`` → ``in_progress`` → ``resolved`` → ``closed``
- Photos are uploaded separately via ``POST /issues/{id}/photos`` after issue creation.
- AI fields (``ai_*``) are populated automatically after photo upload — do not send them.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from app.core.sanitize import sanitize_comment, sanitize_description


# MEDIUM PRIORITY BUG FIX #1: Explicit Enum Validation
class IssueTypeEnum(str, Enum):
    """Valid built-in issue types in the system"""
    garbage = "garbage"
    pothole = "pothole"
    streetlight = "streetlight"
    drain = "drain"
    water = "water"
    other = "other"


class IssueSeverityEnum(str, Enum):
    """Issue severity levels"""
    low = "low"
    medium = "medium"
    high = "high"


class IssuePriorityEnum(str, Enum):
    """Issue priority levels"""
    low = "low"
    medium = "medium"
    high = "high"
    urgent = "urgent"


class IssueStatusEnum(str, Enum):
    """Issue status lifecycle"""
    open = "open"
    assigned = "assigned"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"


class DepartmentEnum(str, Enum):
    """Government departments"""
    water = "water"
    roads = "roads"
    electricity = "electricity"
    sanitation = "sanitation"
    parks = "parks"
    other = "other"


class IssueCreate(BaseModel):
    """Request body for ``POST /issues``.
    
    FIX MEDIUM PRIORITY BUG #1: Missing enum validation on issue_type/severity/priority
    All enums are now strictly validated with explicit Enum types.
    """

    issue_type: IssueTypeEnum = Field(..., description="garbage | pothole | streetlight | drain | water | other")
    custom_issue_type_label: Optional[str] = Field(None, max_length=100, description="Custom label when issue_type is 'other'")
    description: Optional[str] = Field(None, max_length=5000, description="Free-text description of the issue")
    latitude: float = Field(..., ge=-90, le=90, description="GPS latitude of the issue location")
    longitude: float = Field(..., ge=-180, le=180, description="GPS longitude of the issue location")
    address: Optional[str] = Field(None, max_length=500, description="Human-readable address for display")
    ward: Optional[str] = Field(None, max_length=200, description="Ward name/number (falls back to reporter's ward)")
    ward_id: Optional[UUID] = Field(None, description="Structured ward UUID (from /locations/talukas/{id}/wards)")
    severity: Optional[IssueSeverityEnum] = Field(IssueSeverityEnum.medium, description="high | medium | low")
    priority: Optional[IssuePriorityEnum] = Field(IssuePriorityEnum.medium, description="urgent | high | medium | low")
    department: Optional[DepartmentEnum] = Field(None, description="water | roads | electricity | sanitation | parks | other")
    is_sos: bool = Field(False, description="Emergency SOS flag — immediately broadcasts to nearby citizens within 500m")

    @field_validator("description")
    @classmethod
    def sanitize_desc(cls, v: Optional[str]) -> Optional[str]:
        """Sanitize description to prevent XSS attacks"""
        if v is None:
            return None
        sanitized = sanitize_description(v)
        return sanitized


class IssueUpdate(BaseModel):
    """Request body for ``PATCH /issues/{id}``.

    Role-based field restrictions:
    - **Citizens**: can only set ``citizen_rating`` on their own resolved issues.
    - **Workers/Admins**: can update ``status`` and ``resolution_notes``.
    - **Admins only**: can set ``assigned_worker_id`` (triggers reassignment).

    Attributes:
        status:             New status value.
        resolution_notes:   Notes from the worker about the resolution.
        assigned_worker_id: UUID of the worker to assign (admin only).
        citizen_rating:     Satisfaction rating 1-5 (citizen only, on resolved issues).
    """

    status: Optional[IssueStatusEnum] = Field(None, description="open | assigned | in_progress | resolved | closed")
    resolution_notes: Optional[str] = Field(None, max_length=5000, description="Worker's notes about the resolution")
    assigned_worker_id: Optional[UUID] = Field(None, description="Worker UUID to assign (admin only)")
    citizen_rating: Optional[int] = Field(None, ge=1, le=5, description="Satisfaction rating 1-5 (citizen only)")
    priority: Optional[IssuePriorityEnum] = Field(None, description="urgent | high | medium | low (admin only)")
    department: Optional[DepartmentEnum] = Field(None, description="water | roads | electricity | sanitation | parks | other")

    @field_validator("citizen_rating")
    @classmethod
    def validate_rating(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and not (1 <= v <= 5):
            raise ValueError("citizen_rating must be between 1 and 5")
        return v


class IssueReporterInfo(BaseModel):
    """Embedded reporter info within ``IssueResponse``.

    Attributes:
        id:    Reporter's user UUID.
        name:  Reporter's display name.
        phone: Reporter's phone number.
    """

    id: UUID
    name: Optional[str] = None
    phone: Optional[str] = None

    model_config = {"from_attributes": True}


class IssueCommentCreate(BaseModel):
    """Request body for ``POST /issues/{id}/comments``.

    MEDIUM PRIORITY BUG FIX #7: HTML Sanitization - Prevents XSS attacks on comments

    Attributes:
        body:      Comment text content - automatically sanitized to remove XSS vectors
        parent_id: UUID of the parent comment when replying (optional).
        is_internal: Internal note — visible to workers and admins only
    """

    body: str = Field(..., min_length=1, max_length=5000, description="Comment text content")
    parent_id: Optional[UUID] = Field(None, description="Parent comment UUID for replies")
    is_internal: bool = Field(False, description="Internal note — visible to workers and admins only")
    
    @field_validator("body")
    @classmethod
    def sanitize_body(cls, v: str) -> str:
        """Sanitize comment body to prevent XSS attacks"""
        sanitized = sanitize_comment(v)
        if not sanitized:
            raise ValueError("Comment body cannot be empty after sanitization")
        return sanitized


class IssueCommentAuthor(BaseModel):
    """Embedded author info within ``IssueCommentResponse``.

    Attributes:
        id:   Author's user UUID.
        name: Author's display name.
        role: Author's role — ``"citizen"`` | ``"worker"`` | ``"admin"``.
    """

    id: UUID
    name: Optional[str] = None
    role: str

    model_config = {"from_attributes": True}


class IssueCommentResponse(BaseModel):
    """Response model for issue comments.

    Attributes:
        id:         Comment UUID.
        issue_id:   Parent issue UUID.
        author_id:  Author's user UUID.
        parent_id:  Parent comment UUID (null for top-level comments).
        body:       Comment text.
        created_at: Timestamp when comment was posted.
        author:     Embedded author info (name + role).
        replies:    Nested replies to this comment.
    """

    id: UUID
    issue_id: UUID
    author_id: UUID
    parent_id: Optional[UUID] = None
    body: str
    is_internal: bool = False
    created_at: datetime
    author: Optional[IssueCommentAuthor] = None
    replies: List["IssueCommentResponse"] = []

    model_config = {"from_attributes": True}


IssueCommentResponse.model_rebuild()


class IssueResponse(BaseModel):
    """Full issue response returned by most issue endpoints.

    Attributes:
        id:                      Issue UUID.
        reporter_id:             UUID of the citizen who reported the issue.
        assigned_worker_id:      UUID of the assigned worker (null if unassigned).
        issue_type:              Category — garbage, pothole, streetlight, drain, other.
        severity:                Priority — high, medium, low.
        status:                  Current status — open, assigned, in_progress, resolved, closed.
        description:             Free-text description.
        latitude:                GPS latitude.
        longitude:               GPS longitude.
        address:                 Human-readable address.
        ward:                    Ward name/number.
        before_photos:           List of URLs for before-photos.
        after_photos:            List of URLs for after-photos (uploaded by worker on resolution).
        ai_issue_type:           AI-classified issue type (from before-photo analysis).
        ai_severity:             AI-predicted severity.
        ai_confidence:           AI classification confidence score (0.0 - 1.0).
        ai_suggested_description: AI-generated description from photo analysis.
        ai_is_resolved:          AI verification of whether the after-photo shows resolution.
        ai_resolution_quality:   AI quality assessment — ``"good"`` | ``"partial"`` | ``"poor"``.
        ai_resolution_notes:     AI notes on the resolution quality.
        resolution_notes:        Worker's manual resolution notes.
        citizen_rating:          Citizen's satisfaction rating (1-5, null if not rated).
        is_duplicate:            Whether AI flagged this as a duplicate of another issue.
        is_escalated:            Whether the issue has been escalated (auto or manual).
        escalated_at:            Timestamp when escalation occurred.
        is_blocked:              Whether the worker flagged this as blocked.
        blocked_reason:          Reason the task is blocked.
        parent_issue_id:         UUID of the original issue (if this is a duplicate).
        created_at:              When the issue was created.
        updated_at:              Last modification timestamp.
        resolved_at:             When the issue was resolved (null if not resolved).
        reporter:                Embedded reporter info (id, name, phone).
    """

    id: UUID
    reporter_id: UUID
    assigned_worker_id: Optional[UUID] = None
    assigned_worker_name: Optional[str] = None
    issue_type: str
    custom_issue_type_label: Optional[str] = None
    severity: str
    priority: str = "medium"
    status: str
    department: Optional[str] = None
    description: Optional[str] = None
    latitude: float
    longitude: float
    address: Optional[str] = None
    ward: Optional[str] = None
    ward_id: Optional[UUID] = None
    before_photos: List[str]
    after_photos: List[str]
    upvote_count: int = 0
    user_upvoted: bool = False
    ai_issue_type: Optional[str] = None
    ai_severity: Optional[str] = None
    ai_confidence: Optional[float] = None
    ai_suggested_description: Optional[str] = None
    ai_is_resolved: Optional[bool] = None
    ai_resolution_quality: Optional[str] = None
    ai_resolution_notes: Optional[str] = None
    resolution_notes: Optional[str] = None
    citizen_rating: Optional[int] = None
    is_duplicate: bool
    is_escalated: bool
    escalated_at: Optional[datetime] = None
    escalation_level: int = 0
    is_blocked: bool
    blocked_reason: Optional[str] = None
    blocked_at: Optional[datetime] = None
    blocked_by_id: Optional[UUID] = None
    unblocked_at: Optional[datetime] = None
    unblocked_by_id: Optional[UUID] = None
    admin_unblock_note: Optional[str] = None
    block_resolved_by: Optional[str] = None
    blocked_duration_hours: Optional[float] = None  # Calculated on retrieval
    reassignment_count: int = 0
    is_deleted: bool = False
    is_sos: bool = False
    parent_issue_id: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime
    resolved_at: Optional[datetime] = None
    reporter: Optional[IssueReporterInfo] = None

    comment_count: int = 0
    model_config = {"from_attributes": True}


class IssueListResponse(BaseModel):
    """Paginated list of issues.

    Attributes:
        items: List of issues for the current page.
        total: Total number of issues matching the filters.
        limit: Number of items per page (limit parameter).
        offset: Starting position (offset parameter).
    """

    items: List[IssueResponse]
    total: int
    limit: int = 50
    offset: int = 0
