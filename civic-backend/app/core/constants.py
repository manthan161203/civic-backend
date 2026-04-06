"""
Central Configuration and Constants for Civic Application
========================================================

Defines all constants, configuration values, and magic numbers used throughout
the application. Centralizing these values makes them easy to adjust, test, and maintain.

Usage:
    >>> from app.core.constants import (
    ...     MAX_PHOTO_SIZE_MB,
    ...     ISSUE_STATUS_CLOSED,
    ...     SLA_THRESHOLDS,
    ...     RATE_LIMIT_REQUESTS
    ... )
    >>> if file_size_mb > MAX_PHOTO_SIZE_MB:
    ...     raise ValidationError("photo", f"Must be under {MAX_PHOTO_SIZE_MB}MB")
"""

from enum import Enum
from typing import Dict

# ─────────────────────────────────────────────────────────────────────────────
# FILE UPLOAD CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

MAX_PHOTO_SIZE_MB = 10
"""Maximum file size for photo uploads in megabytes."""

MAX_PHOTO_SIZE_BYTES = MAX_PHOTO_SIZE_MB * 1024 * 1024
"""Maximum file size for photo uploads in bytes."""

ALLOWED_PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
"""Allowed file extensions for photo uploads."""

ALLOWED_EXTENSIONS = ALLOWED_PHOTO_EXTENSIONS
"""Alias for allowed photo extensions."""

ALLOWED_PHOTO_MIME_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
"""Allowed MIME types for photo uploads."""

MAX_PHOTOS_PER_ISSUE = 10
"""Maximum number of photos allowed per issue."""

# ─────────────────────────────────────────────────────────────────────────────
# ISSUE STATUS AND PRIORITY ENUMS
# ─────────────────────────────────────────────────────────────────────────────

class IssueStatus(str, Enum):
    """Valid issue status values."""
    OPEN = "open"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class IssuePriority(str, Enum):
    """Valid issue priority values."""
    URGENT = "urgent"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IssueSeverity(str, Enum):
    """Valid issue severity values."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


ISSUE_STATUS_OPEN = IssueStatus.OPEN.value
ISSUE_STATUS_ASSIGNED = IssueStatus.ASSIGNED.value
ISSUE_STATUS_IN_PROGRESS = IssueStatus.IN_PROGRESS.value
ISSUE_STATUS_RESOLVED = IssueStatus.RESOLVED.value
ISSUE_STATUS_CLOSED = IssueStatus.CLOSED.value

ISSUE_COMPLETED_STATUSES = {ISSUE_STATUS_RESOLVED, ISSUE_STATUS_CLOSED}
"""Statuses considered as 'completed' for metrics and leaderboard."""

ISSUE_ACTIVE_STATUSES = {ISSUE_STATUS_OPEN, ISSUE_STATUS_ASSIGNED, ISSUE_STATUS_IN_PROGRESS}
"""Statuses considered as 'active/open' for SLA tracking."""

# Built-in (hardcoded) issue types
BUILT_IN_ISSUE_TYPES = {"garbage", "pothole", "streetlight", "drain", "water", "other"}
"""Built-in issue types that don't require approval."""

# Default values for issue creation
ISSUE_PRIORITY_URGENT = IssuePriority.URGENT.value
ISSUE_PRIORITY_HIGH = IssuePriority.HIGH.value
ISSUE_PRIORITY_MEDIUM = IssuePriority.MEDIUM.value
ISSUE_PRIORITY_LOW = IssuePriority.LOW.value

ISSUE_SEVERITY_HIGH = IssueSeverity.HIGH.value
ISSUE_SEVERITY_MEDIUM = IssueSeverity.MEDIUM.value
ISSUE_SEVERITY_LOW = IssueSeverity.LOW.value

ISSUE_SEVERITY_DEFAULT = ISSUE_SEVERITY_MEDIUM
"""Default severity level for new issues."""

ISSUE_PRIORITY_DEFAULT = ISSUE_PRIORITY_MEDIUM
"""Default priority level for new issues."""

# ─────────────────────────────────────────────────────────────────────────────
# SLA (Service Level Agreement) CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

SLA_THRESHOLDS: Dict[str, int] = {
    "urgent": 24,
    "high": 48,
    "medium": 72,
    "low": 168,  # 7 days
}
"""SLA response time thresholds in hours, by issue priority."""

SLA_WARNING_THRESHOLD_HOURS = 2
"""Number of hours before SLA breach when issue is marked as 'at_risk'."""

AUTO_ESCALATION_THRESHOLDS = [  # (days, level)
    (1, 1),   # After 1 day → escalate to taluka
    (3, 2),   # After 3 days → escalate to district
    (7, 3),   # After 7 days → escalate to state
]
"""Escalation thresholds: (days_open, escalation_level)."""

# ─────────────────────────────────────────────────────────────────────────────
# USER ROLES AND PERMISSIONS
# ─────────────────────────────────────────────────────────────────────────────

class UserRole(str, Enum):
    """Valid user role values."""
    CITIZEN = "citizen"
    WORKER = "worker"
    ADMIN = "admin"
    WARD_ADMIN = "ward_admin"
    TALUKA_ADMIN = "taluka_admin"
    DISTRICT_ADMIN = "district_admin"


ADMIN_ROLES = {
    UserRole.ADMIN.value,
    UserRole.WARD_ADMIN.value,
    UserRole.TALUKA_ADMIN.value,
    UserRole.DISTRICT_ADMIN.value,
}
"""Roles that have administrative privileges."""

WORKER_ROLES = {UserRole.WORKER.value}
"""Roles that perform field work."""

CITIZEN_ROLES = {UserRole.CITIZEN.value}
"""Roles that report issues."""

# ─────────────────────────────────────────────────────────────────────────────
# RATING AND SCORING
# ─────────────────────────────────────────────────────────────────────────────

MIN_CITIZEN_RATING = 1
MAX_CITIZEN_RATING = 5

FIVE_STAR_RATING_THRESHOLD = 5
"""Rating value that triggers reward for worker."""

AI_CONFIDENCE_THRESHOLD = 0.5
"""Minimum AI confidence score (0.0-1.0) to use AI suggestions."""

# ─────────────────────────────────────────────────────────────────────────────
# PAGINATION AND LISTING
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_PAGE_SIZE = 20
"""Default number of items per page for list endpoints."""

MAX_PAGE_SIZE = 200
"""Maximum allowed page size to prevent resource exhaustion."""

MIN_PAGE_SIZE = 1
"""Minimum allowed page size."""

# ─────────────────────────────────────────────────────────────────────────────
# LANGUAGE AND TRANSLATION
# ─────────────────────────────────────────────────────────────────────────────

SUPPORTED_LANGUAGES = {'en', 'gu', 'hi'}
"""Supported languages for multilingual features."""

DEFAULT_LANGUAGE = 'en'
"""Default language for API responses."""

# ─────────────────────────────────────────────────────────────────────────────
# RATE LIMITING AND THROTTLING
# ─────────────────────────────────────────────────────────────────────────────

RATE_LIMIT_REQUESTS = 100
"""Maximum requests per time window."""

RATE_LIMIT_WINDOW_SECONDS = 60
"""Time window for rate limiting in seconds."""

DAILY_ISSUE_LIMIT_CITIZEN = 10
"""Maximum issues a citizen can report per calendar day."""

SYNC_QUEUE_MAX_RETRIES = 3
"""Maximum retry attempts for offline sync queue."""

SYNC_QUEUE_RETRY_DELAY_SECONDS = 30
"""Delay between sync queue retry attempts."""

# ─────────────────────────────────────────────────────────────────────────────
# NOTIFICATION AND MESSAGING
# ─────────────────────────────────────────────────────────────────────────────

SMS_CHARACTER_LIMIT = 160
"""Maximum characters per SMS message."""

NOTIFICATION_BATCH_SIZE = 50
"""Number of notifications to process in a batch."""

SOS_BROADCAST_RADIUS_KM = 5
"""Radius in kilometers for SOS alert broadcasts."""

SOS_NOTIFICATION_LIMIT = 50
"""Maximum citizens to notify for a single SOS."""

# ─────────────────────────────────────────────────────────────────────────────
# DISPUTE AND RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────

class DisputeStatus(str, Enum):
    """Valid dispute status values."""
    OPEN = "open"
    UNDER_REVIEW = "under_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


MAX_DISPUTE_CLOSURE_DAYS = 30
"""Maximum days to close a dispute after creation."""

# ─────────────────────────────────────────────────────────────────────────────
# TOKEN EXPIRATION
# ─────────────────────────────────────────────────────────────────────────────

ACCESS_TOKEN_EXPIRE_MINUTES = 60
"""Access token expiration time in minutes."""

REFRESH_TOKEN_EXPIRE_DAYS = 30
"""Refresh token expiration time in days."""

PASSWORD_RESET_TOKEN_EXPIRE_HOURS = 24
"""Password reset token expiration time in hours."""

# ─────────────────────────────────────────────────────────────────────────────
# OFFLINE QUEUE CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

OFFLINE_QUEUE_MAX_RETRY = 5
"""Maximum attempts to sync queued operations."""

OFFLINE_QUEUE_BATCH_SIZE = 10
"""Number of queued operations to process per batch."""

# ─────────────────────────────────────────────────────────────────────────────
# VALIDATION CONSTRAINTS
# ─────────────────────────────────────────────────────────────────────────────

MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 50

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128

MIN_PHONE_LENGTH = 10
MAX_PHONE_LENGTH = 15

MAX_DESCRIPTION_LENGTH = 5000
MAX_FEEDBACK_LENGTH = 1000

MIN_LATITUDE = -90
MAX_LATITUDE = 90
MIN_LONGITUDE = -180
MAX_LONGITUDE = 180

# ─────────────────────────────────────────────────────────────────────────────
# EMAIL VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

EMAIL_VALIDATION_PATTERN = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
"""Regex pattern for email validation."""

# ─────────────────────────────────────────────────────────────────────────────
# REASSIGNMENT THRESHOLDS
# ─────────────────────────────────────────────────────────────────────────────

AUTO_ESCALATE_AFTER_REJECTIONS = 3
"""Number of rejections before auto-escalation."""

MAX_REASSIGNMENT_COUNT = 5
"""Maximum times an issue can be reassigned before escalation."""

# ─────────────────────────────────────────────────────────────────────────────
# API TIMEOUT CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

API_TIMEOUT_SECONDS = 30
"""Default timeout for external API calls."""

DATABASE_TIMEOUT_SECONDS = 15
"""Timeout for database operations."""

FILE_UPLOAD_TIMEOUT_SECONDS = 120
"""Timeout for file upload operations."""
