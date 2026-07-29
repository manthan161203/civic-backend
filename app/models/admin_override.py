"""
Admin Override Model - Emergency Cross-Scope Access
====================================================
Stores active/granted admin overrides for accessing data outside normal geographic scope.
"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.time import now_utc
from app.database import Base


class AdminOverride(Base):
    """Stores active admin override grants for cross-scope access."""
    
    __tablename__ = "admin_overrides"
    __table_args__ = (
        # Partial index: the hot query is "does this admin have a live
        # override?", so only unrevoked rows need to be indexed.
        Index(
            "ix_admin_overrides_active",
            "granted_to_admin_id",
            "revoked_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    granted_by_admin_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )  # Super-admin who granted
    granted_to_admin_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )  # Admin who received override
    
    # Scope being overridden
    scope_level = Column(String(50), nullable=False)  # 'ward', 'taluka', 'district'
    target_scope_id = Column(UUID(as_uuid=True), nullable=False)  # UUID of the location
    
    # Audit trail
    reason = Column(Text, nullable=False)  # Why override was needed
    override_until = Column(DateTime(timezone=True), nullable=True)  # NULL = permanent, set = temporary
    
    # Revocation
    revoked_at = Column(DateTime(timezone=True), nullable=True, index=True)  # When it was revoked
    revoked_by = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )  # Who revoked it
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    # Relationships (imported at module level to avoid circular imports)
    granted_by_admin = relationship(
        "User",
        foreign_keys=[granted_by_admin_id],
        primaryjoin="AdminOverride.granted_by_admin_id == User.id",
        viewonly=True
    )
    granted_to_admin = relationship(
        "User",
        foreign_keys=[granted_to_admin_id],
        primaryjoin="AdminOverride.granted_to_admin_id == User.id",
        viewonly=True
    )
    revoked_by_admin = relationship(
        "User",
        foreign_keys=[revoked_by],
        primaryjoin="AdminOverride.revoked_by == User.id",
        viewonly=True
    )
    
    def __repr__(self):
        status = "revoked" if self.revoked_at else "active"
        return f"<AdminOverride {self.granted_to_admin_id} → {self.scope_level}:{self.target_scope_id} [{status}]>"
    
    @property
    def is_active(self) -> bool:
        """Check if override is currently active (not revoked and not expired)."""
        if self.revoked_at:
            return False
        if self.override_until:
            return now_utc() < self.override_until
        return True
