"""
AdminOverrideLog — audit trail for cross-scope admin access
===========================================================
Records every time an admin reaches outside their normal geographic scope: who,
what scope, why, which resource, and when the override expires.

The service layer lives in ``app/services/admin_override.py``; the model lives
here so it is registered on ``Base.metadata`` whenever ``app.models`` is
imported. Declared inside the service module it was invisible to Alembic, and
``alembic check`` reported ``admin_override_logs`` as a table to be dropped —
autogenerate would have written that ``DROP TABLE`` into a migration and taken
the audit trail with it.
"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class AdminOverrideLog(Base):
    """Audit log for admin override access to out-of-scope data."""

    __tablename__ = "admin_override_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    admin_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    admin_role = Column(String(50), nullable=False)  # The role of the admin performing override
    target_scope = Column(String(255), nullable=False)  # e.g., "ward:123", "taluka:456"
    reason = Column(String(500), nullable=False)  # Why they needed to access this data
    accessed_resource_type = Column(String(50), nullable=False)  # "issue", "worker", "announcement", etc.
    accessed_resource_id = Column(UUID(as_uuid=True), nullable=True)  # ID of the accessed resource
    override_until = Column(DateTime(timezone=True), nullable=True)  # When override expires (if temporary)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    def __repr__(self):
        return f"<AdminOverrideLog {self.admin_id} -> {self.target_scope} at {self.created_at}>"
