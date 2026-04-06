"""
CustomIssueType Model — User-submitted issue type suggestions
=============================================================
When citizens select "other" as issue_type, they provide a custom label.
Admins can review popular custom types and promote them to official types.
"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class CustomIssueType(Base):
    __tablename__ = "custom_issue_types"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    label = Column(String(100), nullable=False, index=True)
    slug = Column(String(100), nullable=False, unique=True, index=True)
    suggested_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    usage_count = Column(Integer, server_default="1", nullable=False)
    is_approved = Column(Boolean, server_default="false", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    suggester = relationship("User")
