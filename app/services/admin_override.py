"""
Admin Override Service — Emergency Cross-Scope Access
======================================================
Provides mechanisms for admins to temporarily access data outside their normal geographic scope,
with comprehensive audit logging for incident response and emergency situations.

ADMIN ROLES AUDIT - GAP #3: Cross-Admin Data Visibility Override
- Enables ward_admin to access adjacent ward data during incidents
- All override actions are logged for compliance and security auditing
- Requires explicit override flag and reason
"""

import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import Column, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Session

from app.core.logger import get_logger
from app.database import Base
from app.models import User, AdminOverride

logger = get_logger(__name__)


class AdminOverrideLog(Base):
    """Audit log for admin override access to out-of-scope data."""
    
    __tablename__ = "admin_override_logs"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    admin_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    admin_role = Column(String(50), nullable=False)  # The role of the admin performing override
    target_scope = Column(String(255), nullable=False)  # e.g., "ward:123", "taluka:456"
    reason = Column(String(500), nullable=False)  # Why they needed to access this data
    accessed_resource_type = Column(String(50), nullable=False)  # "issue", "worker", "announcement", etc.
    accessed_resource_id = Column(UUID(as_uuid=True), nullable=True)  # ID of the accessed resource
    override_until = Column(DateTime(timezone=True), nullable=True)  # When override expires (if temporary)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    
    def __repr__(self):
        return f"<AdminOverrideLog {self.admin_id} -> {self.target_scope} at {self.created_at}>"


def can_admin_override_access(
    admin: User,
    target_ward_id: Optional[uuid.UUID] = None,
    target_taluka_id: Optional[uuid.UUID] = None,
    target_district_id: Optional[uuid.UUID] = None,
    db: Optional[Session] = None,
) -> bool:
    """
    Check if admin has active override permission for the target scope.
    
    Args:
        admin: The admin user attempting access
        target_ward_id: Ward they're trying to access
        target_taluka_id: Taluka they're trying to access
        target_district_id: District they're trying to access
        db: Database session for checking active overrides
    
    Returns:
        True if override is active and allows access, False otherwise
    """
    if not db:
        return False
    
    # Super-admin never needs override
    if admin.role == "admin":
        return True
    
    # Determine target scope level and ID
    if target_ward_id:
        scope_level = "ward"
        target_scope_id = target_ward_id
    elif target_taluka_id:
        scope_level = "taluka"
        target_scope_id = target_taluka_id
    elif target_district_id:
        scope_level = "district"
        target_scope_id = target_district_id
    else:
        return False
    
    # Check if admin has active override for this scope
    active_override = (
        db.query(AdminOverride)
        .filter(
            AdminOverride.granted_to_admin_id == admin.id,
            AdminOverride.scope_level == scope_level,
            AdminOverride.target_scope_id == target_scope_id,
            AdminOverride.revoked_at.is_(None),  # Not revoked
            (AdminOverride.override_until.is_(None)) |  # Permanent or
            (AdminOverride.override_until > datetime.utcnow())  # Not expired
        )
        .first()
    )
    
    return active_override is not None


def log_override_access(
    admin: User,
    target_scope: str,  # e.g., "ward:123", "taluka:456"
    reason: str,
    resource_type: str,
    resource_id: Optional[uuid.UUID] = None,
    duration_minutes: Optional[int] = None,  # None = permanent until revoked
    db: Optional[Session] = None,
) -> AdminOverrideLog:
    """
    Log an admin's override access for audit trail.
    
    Args:
        admin: The admin performing the override access
        target_scope: What scope they're accessing outside their normal scope
        reason: Reason for override (incident number, emergency, etc.)
        resource_type: Type of resource accessed (issue, worker, announcement, etc.)
        resource_id: ID of specific resource accessed
        duration_minutes: How long override is valid (None = permanent)
        db: Database session
    
    Returns:
        Created AdminOverrideLog entry
    """
    if not db:
        raise ValueError("Database session required for logging override")
    
    override_until = None
    if duration_minutes:
        override_until = datetime.utcnow() + timedelta(minutes=duration_minutes)
    
    log = AdminOverrideLog(
        id=uuid.uuid4(),
        admin_id=admin.id,
        admin_role=admin.role,
        target_scope=target_scope,
        reason=reason,
        accessed_resource_type=resource_type,
        accessed_resource_id=resource_id,
        override_until=override_until,
    )
    
    db.add(log)
    db.commit()
    
    logger.info(
        f"Admin override logged: {admin.id} ({admin.role}) "
        f"accessing {target_scope} for {resource_type}. "
        f"Reason: {reason}. Until: {override_until or 'permanent'}",
        extra={"audit": True}
    )
    
    return log


def revoke_override(override_id: uuid.UUID, db: Session) -> None:
    """
    Revoke an active override (set override_until to now).
    
    Args:
        override_id: ID of override log to revoke
        db: Database session
    """
    override_log = db.query(AdminOverrideLog).filter(AdminOverrideLog.id == override_id).first()
    if override_log:
        override_log.override_until = datetime.utcnow()
        db.commit()
        logger.info(f"Admin override revoked: {override_id}", extra={"audit": True})


def get_active_overrides(admin_id: uuid.UUID, db: Session) -> list:
    """Get all active overrides for an admin."""
    overrides = (
        db.query(AdminOverrideLog)
        .filter(
            AdminOverrideLog.admin_id == admin_id,
            (AdminOverrideLog.override_until.is_(None)) |
            (AdminOverrideLog.override_until > datetime.utcnow())
        )
        .order_by(AdminOverrideLog.created_at.desc())
        .all()
    )
    return overrides


def get_override_log(
    admin_id: Optional[uuid.UUID] = None,
    days: int = 30,
    db: Optional[Session] = None,
) -> list:
    """
    Get override audit log for specified admin or all admins.
    
    Args:
        admin_id: Specific admin to get log for (None = all admins)
        days: How many days back to retrieve
        db: Database session
    
    Returns:
        List of AdminOverrideLog entries
    """
    if not db:
        return []
    
    since = datetime.utcnow() - timedelta(days=days)
    query = db.query(AdminOverrideLog).filter(AdminOverrideLog.created_at >= since)
    
    if admin_id:
        query = query.filter(AdminOverrideLog.admin_id == admin_id)
    
    return query.order_by(AdminOverrideLog.created_at.desc()).all()
