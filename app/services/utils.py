"""
Utility Functions for Common Operations
=======================================

Consolidates frequently used patterns across the codebase to eliminate duplication.
Improves maintainability and consistency.

FETCHING UTILITIES:
    get_issue_or_404: Retrieve issue by ID with error on not found
    get_user_or_404: Retrieve user by ID with error on not found
    get_active_user_or_404: Retrieve active user by ID with error
    get_user_by_phone: Retrieve user by phone number (returns None if not found)
    get_issue_by_id_active: Retrieve active issue by ID (returns None if not found)

USER/ROLE UTILITIES:
    get_users_by_role: Retrieve all users with specific role (e.g., "admin")
    get_users_with_roles: Retrieve all users with any of multiple roles
    user_has_role: Check if user has any of specified roles
    apply_active_filter: Add is_active filter to User query

FILTERING UTILITIES:
    apply_search_filter: Add text search filter across multiple fields
    apply_pagination: Apply validated pagination (offset/limit)
    apply_not_deleted_filter: Add is_deleted filter to Issue query
    apply_active_filter: Add is_active filter to User query

COUNTING/CHECKING UTILITIES:
    count_issues_by_status: Count issues with specific status
    count_issues_by_priority: Count issues with specific priority
    check_resource_exists: Generic check if resource exists

CALCULATION UTILITIES:
    compute_issue_sla: Calculate SLA metrics (hours remaining, status)
    validate_photo_file: Validate photo file size and type

RESPONSE BUILDING UTILITIES:
    build_issue_response: Convert Issue model to API response
    build_user_response: Convert User model to API response

DATABASE TRANSACTION UTILITIES:
    batch_commit: Safely commit transaction with error handling
    batch_rollback: Safely rollback transaction with logging
"""

import uuid
import logging
from typing import Optional, List, Tuple
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session, Query

from app.models.issue import Issue
from app.models.user import User
from app.core.constants import (
    MAX_PHOTO_SIZE_BYTES,
    ALLOWED_PHOTO_EXTENSIONS,
    ALLOWED_PHOTO_MIME_TYPES,
    SLA_THRESHOLDS,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE
)
from app.core.exceptions import (
    ValidationError,
    ResourceNotFoundError,
    DatabaseError
)

logger = logging.getLogger(__name__)


def validate_photo_file(
    filename: str,
    file_size_bytes: int,
    mime_type: str
) -> None:
    """Validate photo file size and type.
    
    Checks that:
    - File extension is allowed (jpg, jpeg, png, webp)
    - MIME type is allowed
    - File size does not exceed maximum
    
    Args:
        filename (str): Name of the file
        file_size_bytes (int): File size in bytes
        mime_type (str): MIME type of the file
        
    Raises:
        ValidationError: If file is invalid
        
    Example:
        >>> validate_photo_file("photo.jpg", 5242880, "image/jpeg")
        >>> # No exception = file is valid
        
        >>> validate_photo_file("photo.pdf", 5242880, "application/pdf")
        >>> # Raises ValidationError: Invalid photo: File type not allowed
    """
    # Check file extension
    file_ext = None
    if '.' in filename:
        file_ext = '.' + filename.rsplit('.', 1)[1].lower()
    
    if not file_ext or file_ext not in ALLOWED_PHOTO_EXTENSIONS:
        raise ValidationError(
            "photo",
            f"File type not allowed. Allowed: {', '.join(ALLOWED_PHOTO_EXTENSIONS)}",
            {"filename": filename, "extension": file_ext}
        )
    
    # Check MIME type
    if mime_type not in ALLOWED_PHOTO_MIME_TYPES:
        raise ValidationError(
            "photo",
            f"MIME type not allowed. Allowed: {', '.join(ALLOWED_PHOTO_MIME_TYPES)}",
            {"mime_type": mime_type}
        )
    
    # Check file size
    if file_size_bytes > MAX_PHOTO_SIZE_BYTES:
        max_mb = MAX_PHOTO_SIZE_BYTES // (1024 * 1024)
        actual_mb = file_size_bytes // (1024 * 1024)
        raise ValidationError(
            "photo",
            f"File size too large ({actual_mb}MB). Maximum: {max_mb}MB",
            {"file_size_bytes": file_size_bytes, "max_size_bytes": MAX_PHOTO_SIZE_BYTES}
        )


def get_issue_or_404(issue_id: uuid.UUID, db: Session) -> Issue:
    """Retrieve issue by ID or raise ResourceNotFoundError.
    
    Common pattern: fetch issue and provide consistent error response.
    
    Args:
        issue_id (uuid.UUID): ID of issue to retrieve
        db (Session): Database session
        
    Returns:
        Issue: The issue object
        
    Raises:
        ResourceNotFoundError: If issue not found or is deleted
        DatabaseError: If database query fails
        
    Example:
        >>> issue = get_issue_or_404(issue_id, db)
        >>> print(f"Found issue: {issue.id}")
    """
    try:
        issue = db.query(Issue).filter(
            Issue.id == issue_id,
            Issue.is_deleted == False
        ).first()
        
        if not issue:
            raise ResourceNotFoundError("Issue", issue_id)
        
        return issue
        
    except ResourceNotFoundError:
        raise
    except Exception as e:
        logger.error(
            f"Error retrieving issue {issue_id}: {str(e)}",
            exc_info=True,
            extra={"issue_id": issue_id}
        )
        raise DatabaseError("query", f"Failed to retrieve issue: {str(e)}")


def get_user_or_404(user_id: uuid.UUID, db: Session) -> User:
    """Retrieve user by ID or raise ResourceNotFoundError.
    
    Common pattern: fetch user and provide consistent error response.
    
    Args:
        user_id (uuid.UUID): ID of user to retrieve
        db (Session): Database session
        
    Returns:
        User: The user object
        
    Raises:
        ResourceNotFoundError: If user not found
        DatabaseError: If database query fails
        
    Example:
        >>> user = get_user_or_404(user_id, db)
        >>> print(f"Found user: {user.name}")
    """
    try:
        user = db.query(User).filter(User.id == user_id).first()
        
        if not user:
            raise ResourceNotFoundError("User", user_id)
        
        return user
        
    except ResourceNotFoundError:
        raise
    except Exception as e:
        logger.error(
            f"Error retrieving user {user_id}: {str(e)}",
            exc_info=True,
            extra={"user_id": user_id}
        )
        raise DatabaseError("query", f"Failed to retrieve user: {str(e)}")


def compute_issue_sla(
    issue_priority: str,
    issue_created_at: datetime
) -> Tuple[int, float, str]:
    """Compute SLA metrics for an issue.
    
    Calculates:
    - SLA threshold (hours based on priority)
    - Hours remaining before breach
    - Status (on_track, warning, breached)
    
    Args:
        issue_priority (str): Issue priority (urgent, high, medium, low)
        issue_created_at (datetime): When issue was created
        
    Returns:
        Tuple[int, float, str]: (sla_hours, hours_remaining, status)
        
    Raises:
        ValidationError: If priority is invalid
        
    Example:
        >>> sla_hrs, remaining, status = compute_issue_sla("high", created_at)
        >>> print(f"SLA: {sla_hrs}h, Remaining: {remaining}h, Status: {status}")
    """
    # Get SLA threshold for priority
    sla_hours = SLA_THRESHOLDS.get(issue_priority)
    if sla_hours is None:
        raise ValidationError(
            "priority",
            f"Invalid priority: {issue_priority}. Must be one of: {', '.join(SLA_THRESHOLDS.keys())}"
        )
    
    # Calculate time remaining
    now = datetime.now(timezone.utc)
    if issue_created_at.tzinfo is None:
        issue_created_at = issue_created_at.replace(tzinfo=timezone.utc)
    
    sla_deadline = issue_created_at + timedelta(hours=sla_hours)
    time_remaining = sla_deadline - now
    hours_remaining = time_remaining.total_seconds() / 3600
    
    # Determine status
    if hours_remaining < 0:
        status = "breached"
    elif hours_remaining < 2:  # Warning threshold
        status = "warning"
    else:
        status = "on_track"
    
    return sla_hours, round(hours_remaining, 1), status


def apply_search_filter(query: Query, search_text: str, fields: List[str]) -> Query:
    """Apply text search filter to query.
    
    Searches across multiple fields using ILIKE (case-insensitive).
    
    Args:
        query (Query): SQLAlchemy query object
        search_text (str): Search text to filter by
        fields (List[str]): List of field objects to search (e.g., [Issue.description, User.name])
        
    Returns:
        Query: Modified query with search filter applied
        
    Example:
        >>> from sqlalchemy import or_
        >>> query = db.query(Issue)
        >>> search_fields = [Issue.description, Issue.address, Issue.ward]
        >>> query = apply_search_filter(query, "pothole", search_fields)
    """
    if not search_text or not fields:
        return query
    
    search_text = search_text.strip()
    if not search_text:
        return query
    
    from sqlalchemy import or_
    
    search_conditions = [field.ilike(f"%{search_text}%") for field in fields]
    return query.filter(or_(*search_conditions))


def apply_pagination(
    query: Query,
    page: int,
    size: int,
    max_size: int = MAX_PAGE_SIZE,
    default_size: int = DEFAULT_PAGE_SIZE
) -> Tuple[Query, int, int]:
    """Apply pagination to query.
    
    Validates page and size, then applies offset/limit.
    
    Args:
        query (Query): SQLAlchemy query object
        page (int): Page number (1-indexed)
        size (int): Items per page
        max_size (int): Maximum allowed page size (default 200)
        default_size (int): Default page size if not specified (default 20)
        
    Returns:
        Tuple[Query, int, int]: (paginated_query, page, size)
        
    Raises:
        ValidationError: If page or size is invalid
        
    Example:
        >>> query = db.query(Issue)
        >>> query, page, size = apply_pagination(query, 2, 20)
        >>> issues = query.all()
        >>> total = db.query(Issue).count()
        >>> print(f"Page {page} of {(total + size - 1) // size}")
    """
    # Validate page
    if page < 1:
        raise ValidationError("page", "Page must be >= 1")
    
    # Validate and apply size
    if size is None or size == 0:
        size = default_size
    
    if size < 1:
        raise ValidationError("size", "Size must be >= 1")
    
    if size > max_size:
        raise ValidationError(
            "size",
            f"Size must be <= {max_size}",
            {"requested_size": size, "max_size": max_size}
        )
    
    # Apply pagination
    offset = (page - 1) * size
    paginated_query = query.offset(offset).limit(size)
    
    return paginated_query, page, size


def build_issue_response(issue: Issue) -> dict:
    """Build standardized issue response object.
    
    Converts Issue model to API response format with all necessary fields.
    This eliminates duplication of the response building logic.
    
    Args:
        issue (Issue): Issue model instance
        
    Returns:
        dict: Properly formatted issue response
        
    Example:
        >>> issue = db.query(Issue).first()
        >>> response = build_issue_response(issue)
        >>> print(response["id"], response["status"])
    """
    return {
        "id": str(issue.id),
        "reporter_id": str(issue.reporter_id),
        "assigned_worker_id": str(issue.assigned_worker_id) if issue.assigned_worker_id else None,
        "assigned_worker_name": issue.assigned_worker_name,
        "issue_type": issue.issue_type,
        "status": issue.status,
        "priority": issue.priority,
        "severity": issue.severity,
        "description": issue.description,
        "address": issue.address,
        "ward": issue.ward,
        "latitude": issue.latitude,
        "longitude": issue.longitude,
        "citizen_rating": issue.citizen_rating,
        "created_at": issue.created_at.isoformat() if issue.created_at else None,
        "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
        "resolved_at": issue.resolved_at.isoformat() if issue.resolved_at else None,
    }


def build_user_response(user: User, include_sensitive: bool = False) -> dict:
    """Build standardized user response object.
    
    Converts User model to API response format, optionally including sensitive fields.
    
    Args:
        user (User): User model instance
        include_sensitive (bool): Whether to include sensitive fields (email, phone)
        
    Returns:
        dict: Properly formatted user response
        
    Example:
        >>> user = db.query(User).first()
        >>> response = build_user_response(user, include_sensitive=False)
        >>> print(response["name"], response["role"])
    """
    response = {
        "id": str(user.id),
        "name": user.name,
        "role": user.role,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }
    
    if include_sensitive:
        response.update({
            "email": user.email,
            "phone": user.phone,
        })
    
    return response


def batch_commit(db: Session, context: str) -> None:
    """Safely commit database transaction with error handling.
    
    Args:
        db (Session): Database session
        context (str): Context description for error logging
        
    Raises:
        DatabaseError: If commit fails
        
    Example:
        >>> db.add(issue)
        >>> batch_commit(db, "create_issue")
    """
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(
            f"Database commit failed in {context}: {str(e)}",
            exc_info=True,
            extra={"context": context}
        )
        raise DatabaseError(
            "commit",
            f"Failed to save changes in {context}: {str(e)}"
        )


def batch_rollback(db: Session, context: str) -> None:
    """Safely rollback database transaction with logging.
    
    Args:
        db (Session): Database session
        context (str): Context description for error logging
    """
    try:
        db.rollback()
        logger.info(f"Database transaction rolled back in {context}")
    except Exception as e:
        logger.error(
            f"Error rolling back transaction in {context}: {str(e)}",
            exc_info=True,
            extra={"context": context}
        )


# ═════════════════════════════════════════════════════════════════════════════
# NEW UTILITY FUNCTIONS - CONSOLIDATE DUPLICATED PATTERNS
# ═════════════════════════════════════════════════════════════════════════════


def get_active_user_or_404(user_id: uuid.UUID, db: Session) -> User:
    """Retrieve active (non-deleted) user by ID or raise ResourceNotFoundError.
    
    Consolidates pattern: `db.query(User).filter(User.id == user_id, User.is_active == True).first()`
    Used across: core/deps.py, notification_service.py, and multiple routes.
    
    Args:
        user_id (uuid.UUID): ID of user to retrieve
        db (Session): Database session
        
    Returns:
        User: The active user object
        
    Raises:
        ResourceNotFoundError: If user not found or is inactive
        DatabaseError: If database query fails
        
    Example:
        >>> user = get_active_user_or_404(user_id, db)
        >>> print(f"User {user.name} is active")
    """
    try:
        user = db.query(User).filter(
            User.id == user_id,
            User.is_active == True
        ).first()
        
        if not user:
            raise ResourceNotFoundError("User", user_id)
        
        return user
        
    except ResourceNotFoundError:
        raise
    except Exception as e:
        logger.error(
            f"Error retrieving active user {user_id}: {str(e)}",
            exc_info=True,
            extra={"user_id": user_id}
        )
        raise DatabaseError("query", f"Failed to retrieve user: {str(e)}")


def get_users_by_role(
    role: str,
    db: Session,
    active_only: bool = True
) -> List[User]:
    """Retrieve all users with a specific role.
    
    Consolidates pattern: `db.query(User).filter(User.role == role, User.is_active == True).all()`
    Used across: workers.py, rewards_service.py, features.py, main.py.
    
    Args:
        role (str): Role to filter by (admin, worker, citizen, etc.)
        db (Session): Database session
        active_only (bool): Whether to only return active users (default: True)
        
    Returns:
        List[User]: List of users with the specified role
        
    Example:
        >>> admins = get_users_by_role("admin", db)
        >>> workers = get_users_by_role("worker", db, active_only=True)
        >>> print(f"Found {len(admins)} active admins")
    """
    query = db.query(User).filter(User.role == role)
    
    if active_only:
        query = query.filter(User.is_active == True)
    
    return query.all()


def get_users_with_roles(
    roles: List[str],
    db: Session,
    active_only: bool = True
) -> List[User]:
    """Retrieve all users with any of the specified roles.
    
    Consolidates pattern: `db.query(User).filter(User.role.in_(roles), User.is_active == True).all()`
    Used across: main.py (multiple admin role queries).
    
    Args:
        roles (List[str]): List of roles to filter by
        db (Session): Database session
        active_only (bool): Whether to only return active users (default: True)
        
    Returns:
        List[User]: List of users matching any of the specified roles
        
    Example:
        >>> admins = get_users_with_roles(["admin", "ward_admin", "taluka_admin"], db)
        >>> print(f"Found {len(admins)} admins with any role")
    """
    query = db.query(User).filter(User.role.in_(roles))
    
    if active_only:
        query = query.filter(User.is_active == True)
    
    return query.all()


def get_user_by_phone(phone: str, db: Session) -> Optional[User]:
    """Retrieve user by phone number.
    
    Consolidates pattern: `db.query(User).filter(User.phone == phone).first()`
    Used across: setup.py, create_admin.py.
    
    Args:
        phone (str): Phone number to search for
        db (Session): Database session
        
    Returns:
        Optional[User]: User object if found, None otherwise
        
    Example:
        >>> user = get_user_by_phone("+91-9876543210", db)
        >>> if user:
        ...     print(f"User exists: {user.name}")
    """
    try:
        return db.query(User).filter(User.phone == phone).first()
    except Exception as e:
        logger.error(
            f"Error querying user by phone: {str(e)}",
            exc_info=True
        )
        raise DatabaseError("query", f"Failed to query user by phone: {str(e)}")


def get_issue_by_id_active(issue_id: uuid.UUID, db: Session) -> Optional[Issue]:
    """Retrieve issue by ID (soft-delete aware) without raising exception.
    
    Consolidates pattern: `db.query(Issue).filter(Issue.id == issue_id, Issue.is_deleted == False).first()`
    Used across: citizen_features.py, features.py, chat.py.
    
    Args:
        issue_id (uuid.UUID): ID of issue to retrieve
        db (Session): Database session
        
    Returns:
        Optional[Issue]: Issue object if found and not deleted, None otherwise
        
    Example:
        >>> issue = get_issue_by_id_active(issue_id, db)
        >>> if issue:
        ...     print(f"Issue exists: {issue.issue_type}")
    """
    try:
        return db.query(Issue).filter(
            Issue.id == issue_id,
            Issue.is_deleted == False
        ).first()
    except Exception as e:
        logger.error(
            f"Error retrieving issue {issue_id}: {str(e)}",
            exc_info=True
        )
        raise DatabaseError("query", f"Failed to retrieve issue: {str(e)}")


def user_has_role(user: User, roles: List[str]) -> bool:
    """Check if user has any of the specified roles.
    
    Consolidates pattern: `user.role == "role"` checks scattered throughout code.
    
    Args:
        user (User): User object to check
        roles (List[str]): List of roles to check against
        
    Returns:
        bool: True if user has any of the specified roles
        
    Example:
        >>> if user_has_role(current_user, ["admin", "ward_admin"]):
        ...     return admin_dashboard
    """
    return user.role in roles


def apply_active_filter(query: Query, active_only: bool = True) -> Query:
    """Apply is_active filter to User query.
    
    Consolidates pattern: `.filter(User.is_active == True)`
    
    Args:
        query (Query): SQLAlchemy User query
        active_only (bool): Whether to filter for active users (default: True)
        
    Returns:
        Query: Filtered query
        
    Example:
        >>> query = db.query(User).filter(User.role == "worker")
        >>> query = apply_active_filter(query)
        >>> workers = query.all()
    """
    if active_only:
        query = query.filter(User.is_active == True)
    return query


def apply_not_deleted_filter(query: Query, not_deleted: bool = True) -> Query:
    """Apply is_deleted filter to Issue query.
    
    Consolidates pattern: `.filter(Issue.is_deleted == False)`
    
    Args:
        query (Query): SQLAlchemy Issue query
        not_deleted (bool): Whether to filter for non-deleted issues (default: True)
        
    Returns:
        Query: Filtered query
        
    Example:
        >>> query = db.query(Issue).filter(Issue.status == "open")
        >>> query = apply_not_deleted_filter(query)
        >>> issues = query.all()
    """
    if not_deleted:
        query = query.filter(Issue.is_deleted == False)
    return query


def count_issues_by_status(
    status: str,
    db: Session,
    user: Optional[User] = None
) -> int:
    """Count issues with specified status (optionally filtered by user scope).
    
    Consolidates pattern: `.filter(Issue.status == status).count()`
Used across: admin routes, worker routes, dashboard routes.
    
    Args:
        status (str): Issue status to count
        db (Session): Database session
        user (Optional[User]): If provided, only count issues visible to this user
        
    Returns:
        int: Number of matching issues
        
    Example:
        >>> open_count = count_issues_by_status("open", db)
        >>> my_issues = count_issues_by_status("assigned", db, current_worker)
    """
    try:
        query = db.query(Issue).filter(
            Issue.status == status,
            Issue.is_deleted == False
        )
        
        if user:
            if user.role == "citizen":
                query = query.filter(Issue.reporter_id == user.id)
            elif user.role == "worker":
                query = query.filter(Issue.assigned_worker_id == user.id)
        
        return query.count()
        
    except Exception as e:
        logger.error(
            f"Error counting issues with status {status}: {str(e)}",
            exc_info=True
        )
        raise DatabaseError("query", f"Failed to count issues: {str(e)}")


def count_issues_by_priority(
    priority: str,
    db: Session
) -> int:
    """Count issues with specified priority.
    
    Consolidates pattern: `db.query(Issue).filter(Issue.priority == priority).count()`
    Used across: dashboard routes, analytics routes.
    
    Args:
        priority (str): Issue priority to count (urgent, high, medium, low)
        db (Session): Database session
        
    Returns:
        int: Number of issues with the specified priority
        
    Example:
        >>> urgent_count = count_issues_by_priority("urgent", db)
    """
    try:
        return db.query(Issue).filter(
            Issue.priority == priority,
            Issue.is_deleted == False
        ).count()
    except Exception as e:
        logger.error(
            f"Error counting issues with priority {priority}: {str(e)}",
            exc_info=True
        )
        raise DatabaseError("query", f"Failed to count issues: {str(e)}")


def check_resource_exists(model, filter_condition, db: Session) -> bool:
    """Generic check if a resource exists matching the filter condition.
    
    Consolidates pattern: `.filter(...).first() is not None`
    Used across: setup.py, features.py, citizen_features.py.
    
    Args:
        model: SQLAlchemy model class
        filter_condition: SQLAlchemy filter expression
        db (Session): Database session
        
    Returns:
        bool: True if resource exists, False otherwise
        
    Example:
        >>> exists = check_resource_exists(User, User.phone == phone, db)
        >>> if not exists:
        ...     create_user(phone)
    """
    try:
        return db.query(model).filter(filter_condition).first() is not None
    except Exception as e:
        logger.error(
            f"Error checking resource existence: {str(e)}",
            exc_info=True
        )
        raise DatabaseError("query", f"Failed to check resource: {str(e)}")
